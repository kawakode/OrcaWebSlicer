#include <catch2/catch_all.hpp>

#include "libslic3r/Web/ProfileCompatibility.hpp"
#include "test_utils.hpp"

#include <filesystem>
#include <fstream>
#include <string>
#include <vector>

#include <nlohmann/json.hpp>

using namespace Slic3r::Web;

namespace {

void write_file(const std::filesystem::path &path, const std::string &contents)
{
    std::filesystem::create_directories(path.parent_path());
    std::ofstream output(path, std::ios::binary | std::ios::trunc);
    REQUIRE(output.good());
    output << contents;
}

// One printer profile shaped like a resolved bundled machine profile: the
// catalog flattens inheritance before the evaluator ever sees it.
void write_printer(const std::filesystem::path &root, const std::string &relative, const std::string &notes,
                   const std::string &nozzles = R"(["0.4"])")
{
    write_file(root / relative, R"({"type":"machine","name":"Printer","from":"system","instantiation":"true",)"
                               R"("printer_notes":")" + notes + R"(","nozzle_diameter":)" + nozzles + "}");
}

std::string request_for(const std::vector<std::string> &printers, const std::string &condition)
{
    nlohmann::json described = nlohmann::json::array();
    for (std::size_t index = 0; index < printers.size(); ++index)
        described.push_back({{"id", printers[index]},
                             {"name", printers[index]},
                             {"profile", "printers/" + std::to_string(index) + ".json"}});
    return nlohmann::json{{"catalog_version", COMPATIBILITY_REQUEST_VERSION},
                          {"printers", described},
                          {"candidates", nlohmann::json::array({{{"id", "candidate"}, {"condition", condition}}})}}
        .dump();
}

nlohmann::json evaluate(const std::filesystem::path &root, const std::string &request)
{
    std::string          response;
    WorkerManifestError  error;
    REQUIRE(evaluate_profile_compatibility(request, root, response, error));
    return nlohmann::json::parse(response);
}

} // namespace

TEST_CASE("A printer condition selects only the printers it matches", "[ProfileCompatibility]")
{
    const ScopedTemporaryDir directory("orca-compatibility");
    const std::filesystem::path root(directory.string());
    write_printer(root, "printers/0.json", "PRINTER_MODEL_KOBRA");
    write_printer(root, "printers/1.json", "PRINTER_MODEL_VYPER");

    const nlohmann::json answered =
        evaluate(root, request_for({"kobra", "vyper"}, "printer_notes=~/.*PRINTER_MODEL_KOBRA.*/"));

    REQUIRE(answered.at("catalog_version") == COMPATIBILITY_REQUEST_VERSION);
    CHECK(answered.at("compatibility").at("candidate") == nlohmann::json::array({"kobra"}));
    CHECK(answered.at("unevaluated").empty());
}

TEST_CASE("A condition reads the selected printer's name and extruder count", "[ProfileCompatibility]")
{
    const ScopedTemporaryDir directory("orca-compatibility");
    const std::filesystem::path root(directory.string());
    write_printer(root, "printers/0.json", "", R"(["0.4"])");
    write_printer(root, "printers/1.json", "", R"(["0.4","0.4"])");

    CHECK(evaluate(root, request_for({"single", "dual"}, "num_extruders > 1"))
              .at("compatibility")
              .at("candidate") == nlohmann::json::array({"dual"}));
    CHECK(evaluate(root, request_for({"single", "dual"}, "printer_preset =~ /single/"))
              .at("compatibility")
              .at("candidate") == nlohmann::json::array({"single"}));
}

TEST_CASE("An unparsable condition suits every printer and is reported", "[ProfileCompatibility]")
{
    const ScopedTemporaryDir directory("orca-compatibility");
    const std::filesystem::path root(directory.string());
    write_printer(root, "printers/0.json", "");

    // The desktop treats a broken condition as "compatible with everything";
    // the API is told which ones did that so the bundle can be fixed.
    const nlohmann::json answered = evaluate(root, request_for({"only"}, "this is not an expression("));

    CHECK(answered.at("compatibility").at("candidate") == nlohmann::json::array({"only"}));
    REQUIRE(answered.at("unevaluated").size() == 1);
    CHECK(answered.at("unevaluated")[0].at("id") == "candidate");
}

TEST_CASE("A compatibility request outside its root is refused", "[ProfileCompatibility]")
{
    const ScopedTemporaryDir directory("orca-compatibility");
    const std::filesystem::path root(directory.string());
    write_printer(root, "printers/0.json", "");
    const std::string escaping =
        nlohmann::json{{"catalog_version", COMPATIBILITY_REQUEST_VERSION},
                       {"printers", nlohmann::json::array({{{"id", "escape"},
                                                            {"name", "escape"},
                                                            {"profile", "../outside.json"}}})},
                       {"candidates", nlohmann::json::array()}}
            .dump();

    std::string         response;
    WorkerManifestError error;
    REQUIRE_FALSE(evaluate_profile_compatibility(escaping, root, response, error));
    CHECK(error.category == WorkerErrorCategory::Profile);
}

TEST_CASE("A compatibility request of the wrong version is refused", "[ProfileCompatibility]")
{
    const ScopedTemporaryDir directory("orca-compatibility");
    const std::filesystem::path root(directory.string());

    std::string         response;
    WorkerManifestError error;
    REQUIRE_FALSE(evaluate_profile_compatibility(
        R"({"catalog_version":99,"printers":[],"candidates":[]})", root, response, error));
    CHECK(error.code == "unsupported_catalog_version");

    REQUIRE_FALSE(evaluate_profile_compatibility("not json", root, response, error));
    CHECK(error.code == "invalid_compatibility_request");
}
