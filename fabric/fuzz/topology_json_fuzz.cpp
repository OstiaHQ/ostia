// libFuzzer target for the fixture JSON path: schema validation, then the model builder and
// topo1 on documents the schema accepts. Input: one selector byte, then a JSON document.
// Selector % 3 picks nvml.json, nics.json or links.json; the seeds use '0', '1' and '2'.
//
// Only the errors these functions document are expected: TopologyError, and a parse failure.
// Anything else on a schema-valid document (a json::type_error, an assert, a sanitizer report)
// is a bug, because the schema is what the builder relies on (RFC-0003 §2).

#include <cstddef>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <nlohmann/json.hpp>
#include <optional>
#include <string_view>

#include "topology/builder.hpp"
#include "topology/error.hpp"
#include "topology/fixture_source.hpp"
#include "topology/identity.hpp"
#include "topology/schema.hpp"

using namespace ostia::fabric::topology;
using nlohmann::json;

namespace {

constexpr std::string_view kNames[] = {"nvml", "nics", "links"};

// The fuzzed document replaces one file of this fixture, so build() sees real PCI facts and
// bus IDs the fuzzer can reach through the seeds.
std::optional<Facts> base;

} // namespace

extern "C" int LLVMFuzzerInitialize(int*, char***) {
    try {
        base = FixtureSource(OSTIA_FUZZ_BASE_FIXTURE).facts();
    } catch (const TopologyError& e) {
        std::fprintf(stderr, "cannot load the base fixture: %s\n", e.what());
        std::abort();
    }
    return 0;
}

extern "C" int LLVMFuzzerTestOneInput(const std::uint8_t* data, std::size_t size) {
    if (size < 1) {
        return -1;
    }
    const std::string_view name = kNames[data[0] % 3];
    const json doc = json::parse(data + 1, data + size, nullptr, false);
    if (doc.is_discarded()) {
        return -1;
    }
    if (!validate(name, doc).empty()) {
        return 0;
    }

    Facts facts = *base;
    if (name == "nvml") {
        facts.nvml = doc;
    } else if (name == "nics") {
        facts.nics = doc;
    } else {
        facts.links = doc;
    }
    try {
        topo1(build(facts));
    } catch (const TopologyError&) {
    }
    return 0;
}
