#include "topology/schema.hpp"

#include <algorithm>
#include <cstddef>
#include <mutex>
#include <regex>
#include <utility>

#include "topology/error.hpp"

namespace ostia::fabric::topology {

namespace {

using nlohmann::json;
using Errors = std::vector<SchemaError>;

// Decision T2: a deliberate subset of JSON Schema, only what the fixture schemas need.
// A test rejects any other keyword in an embedded schema.
const std::map<std::string_view, json>& load() {
    static const std::map<std::string_view, json> schemas = [] {
        static const std::pair<std::string_view, std::string_view> sources[] = {
#include "schemas.inc"
        };
        std::map<std::string_view, json> parsed;
        for (const auto& [name, text] : sources) {
            parsed.emplace(name, json::parse(text));
        }
        return parsed;
    }();
    return schemas;
}

std::string escape(const std::string& token) {
    std::string out;
    for (char c : token) {
        if (c == '~') {
            out += "~0";
        } else if (c == '/') {
            out += "~1";
        } else {
            out += c;
        }
    }
    return out;
}

const std::regex& compiled(const std::string& pattern) {
    static std::mutex mutex;
    static std::map<std::string, std::regex> cache;
    std::lock_guard lock(mutex);
    auto it = cache.find(pattern);
    if (it == cache.end()) {
        it = cache.emplace(pattern, std::regex(pattern, std::regex::ECMAScript)).first;
    }
    return it->second; // std::map nodes are stable, so the reference outlives the lock
}

bool has_type(const json& doc, const std::string& type) {
    if (type == "object")
        return doc.is_object();
    if (type == "array")
        return doc.is_array();
    if (type == "string")
        return doc.is_string();
    if (type == "integer")
        return doc.is_number_integer();
    if (type == "number")
        return doc.is_number();
    if (type == "boolean")
        return doc.is_boolean();
    if (type == "null")
        return doc.is_null();
    return false;
}

void check(const json& schema, const json& doc, const std::string& path, Errors& errors);

void check_one_of(const json& branches, const json& doc, const std::string& path, Errors& errors) {
    std::size_t passing = 0;
    Errors fewest;
    bool have_fewest = false;
    for (const auto& branch : branches) {
        Errors branch_errors;
        check(branch, doc, path, branch_errors);
        if (branch_errors.empty()) {
            ++passing;
        } else if (!have_fewest || branch_errors.size() < fewest.size()) {
            fewest = std::move(branch_errors);
            have_fewest = true;
        }
    }
    if (passing == 1)
        return;
    if (passing > 1) {
        errors.push_back({path, "matches more than one oneOf branch"});
        return;
    }
    errors.insert(errors.end(), fewest.begin(), fewest.end());
}

void check(const json& schema, const json& doc, const std::string& path, Errors& errors) {
    if (schema.contains("type")) {
        const json& t = schema["type"];
        const bool ok = t.is_array() ? std::any_of(t.begin(), t.end(),
                                                   [&](const json& one) {
                                                       return has_type(doc, one.get<std::string>());
                                                   })
                                     : has_type(doc, t.get<std::string>());
        if (!ok) {
            errors.push_back({path, "expected type " + t.dump() + ", got " + doc.type_name()});
            return; // the keywords below assume the type matched
        }
    }
    if (schema.contains("const") && doc != schema["const"]) {
        errors.push_back({path, "expected " + schema["const"].dump() + ", got " + doc.dump()});
    }
    if (schema.contains("enum")) {
        const json& values = schema["enum"];
        if (std::find(values.begin(), values.end(), doc) == values.end()) {
            errors.push_back({path, "expected one of " + values.dump() + ", got " + doc.dump()});
        }
    }
    if (schema.contains("oneOf"))
        check_one_of(schema["oneOf"], doc, path, errors);
    if (schema.contains("pattern") && doc.is_string() &&
        !std::regex_search(doc.get<std::string>(),
                           compiled(schema["pattern"].get<std::string>()))) {
        errors.push_back({path, "does not match pattern " + schema["pattern"].dump()});
    }
    if (doc.is_number()) {
        if (schema.contains("minimum") && doc.get<double>() < schema["minimum"].get<double>()) {
            errors.push_back({path, "below minimum " + schema["minimum"].dump()});
        }
        if (schema.contains("maximum") && doc.get<double>() > schema["maximum"].get<double>()) {
            errors.push_back({path, "above maximum " + schema["maximum"].dump()});
        }
    }
    if (doc.is_object()) {
        const json props = schema.value("properties", json::object());
        for (const auto& name : schema.value("required", json::array())) {
            if (!doc.contains(name.get<std::string>())) {
                errors.push_back({path, "missing required property " + name.dump()});
            }
        }
        for (const auto& [key, value] : doc.items()) {
            const std::string child = path + "/" + escape(key);
            if (props.contains(key)) {
                check(props[key], value, child, errors);
            } else if (schema.value("additionalProperties", true) == false) {
                errors.push_back({child, "unexpected property"});
            }
        }
    }
    if (doc.is_array() && schema.contains("items")) {
        for (std::size_t i = 0; i < doc.size(); ++i) {
            check(schema["items"], doc[i], path + "/" + std::to_string(i), errors);
        }
    }
}

} // namespace

const std::map<std::string_view, json>& embedded_schemas() { return load(); }

std::vector<SchemaError> validate(std::string_view name, const json& doc) {
    const auto& schemas = load();
    const auto it = schemas.find(name);
    if (it == schemas.end()) {
        throw TopologyError("schema", "", "unknown schema '" + std::string(name) + "'");
    }
    Errors errors;
    check(it->second, doc, "", errors);
    return errors;
}

} // namespace ostia::fabric::topology
