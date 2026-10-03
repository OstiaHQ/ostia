// libFuzzer target for the fixture JSON path: schema validation, then the model builder and
// topo1 on documents the schema accepts. Input: one selector byte, then a JSON document.
// Selector % 3 picks nvml.json, nics.json or links.json; the seeds use '0', '1' and '2'.
//
// Only the errors these functions document are expected: TopologyError, and a parse failure.
// Anything else on a schema-valid document (a json::type_error, an assert, a sanitizer report)
// is a bug, because the schema is what the builder relies on (RFC-0003 §2).
//
// The custom mutator edits one value of the parsed document and reserializes it. Byte-level
// mutation alone rarely keeps the JSON schema-valid and cannot move a text integer past a
// threshold in one step; libFuzzer's own mutators still handle a share of the inputs.

#include <cstddef>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <iterator>
#include <limits>
#include <nlohmann/json.hpp>
#include <optional>
#include <random>
#include <string>
#include <string_view>
#include <utility>
#include <vector>

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

// The base fixture's PCI device bus IDs: a mutated reference that names one resolves, so the
// builder gets past its dangling-reference checks.
std::vector<std::string> bus_ids;

using Rng = std::minstd_rand;

std::size_t pick(Rng& rng, std::size_t n) {
    return std::uniform_int_distribution<std::size_t>(0, n - 1)(rng);
}

void collect(json& j, std::vector<json*>& out) {
    out.push_back(&j);
    if (j.is_structured()) {
        for (auto& child : j) {
            collect(child, out);
        }
    }
}

// Unsigned arithmetic, so v + 1 at the int64 limit wraps instead of being undefined.
std::int64_t mutate_integer(Rng& rng, std::int64_t v) {
    const auto u = static_cast<std::uint64_t>(v);
    switch (pick(rng, 9)) {
    case 0:
        return static_cast<std::int64_t>(u + 1);
    case 1:
        return static_cast<std::int64_t>(u - 1);
    case 2:
        return 0;
    case 3:
        return -1;
    case 4:
        return static_cast<std::int64_t>(u * 2);
    case 5:
        return std::numeric_limits<std::int64_t>::max();
    case 6:
        return std::numeric_limits<std::int64_t>::min();
    case 7:
        return std::int64_t{1} << pick(rng, 63);
    default:
        return static_cast<std::int64_t>((std::uint64_t{rng()} << 32) ^ rng());
    }
}

std::string mutate_string(Rng& rng, std::string s) {
    static const char* const kTokens[] = {"",         "unknown",   "active", "inactive",
                                          "gpu",      "switch",    "ok",     "not_supported",
                                          "ethernet", "infiniband"};
    switch (pick(rng, 4)) {
    case 0:
        return bus_ids.empty() ? s : bus_ids[pick(rng, bus_ids.size())];
    case 1:
        return kTokens[pick(rng, std::size(kTokens))];
    case 2:
        if (!s.empty()) {
            s.erase(pick(rng, s.size()), 1);
        }
        return s;
    default:
        s.insert(s.begin() + static_cast<std::ptrdiff_t>(pick(rng, s.size() + 1)),
                 static_cast<char>(pick(rng, 128)));
        return s;
    }
}

void mutate_value(Rng& rng, json& j) {
    if (j.is_number_integer()) {
        j = mutate_integer(rng, j.get<std::int64_t>());
    } else if (j.is_string()) {
        j = mutate_string(rng, j.get<std::string>());
    } else if (j.is_boolean()) {
        j = !j.get<bool>();
    } else if (j.is_array() && !j.empty()) {
        const std::size_t i = pick(rng, j.size());
        switch (pick(rng, 3)) {
        case 0: {
            json copy = j[i]; // insert() may reallocate the array j[i] lives in
            j.insert(j.begin() + static_cast<std::ptrdiff_t>(i), std::move(copy));
            break;
        }
        case 1:
            j.erase(i);
            break;
        default:
            std::swap(j[i], j[pick(rng, j.size())]);
        }
    } else if (j.is_object() && !j.empty()) {
        auto it = j.begin();
        std::advance(it, static_cast<std::ptrdiff_t>(pick(rng, j.size())));
        j.erase(it);
    } else {
        j = mutate_integer(rng, 0);
    }
}

} // namespace

extern "C" int LLVMFuzzerInitialize(int*, char***) {
    try {
        base = FixtureSource(OSTIA_FUZZ_BASE_FIXTURE).facts();
        for (const auto& p : base->pci) {
            if (!p.bridge && !p.bus_id.empty()) {
                bus_ids.push_back(p.bus_id);
            }
        }
    } catch (const TopologyError& e) {
        std::fprintf(stderr, "cannot load the base fixture: %s\n", e.what());
        std::abort();
    }
    return 0;
}

extern "C" std::size_t LLVMFuzzerMutate(std::uint8_t* data, std::size_t size, std::size_t max_size);

extern "C" std::size_t LLVMFuzzerCustomMutator(std::uint8_t* data, std::size_t size,
                                               std::size_t max_size, unsigned int seed) {
    Rng rng(seed);
    if (size < 2 || pick(rng, 4) == 0) {
        return LLVMFuzzerMutate(data, size, max_size);
    }
    json doc = json::parse(data + 1, data + size, nullptr, false);
    if (doc.is_discarded()) {
        return LLVMFuzzerMutate(data, size, max_size);
    }
    std::vector<json*> nodes;
    collect(doc, nodes);
    mutate_value(rng, *nodes[pick(rng, nodes.size())]);
    // Erasing one byte of a multi-byte character leaves invalid UTF-8, which dump() throws on
    // by default; replace keeps the mutation and the output valid.
    const std::string text = doc.dump(-1, ' ', false, json::error_handler_t::replace);
    if (text.size() + 1 > max_size) {
        return LLVMFuzzerMutate(data, size, max_size);
    }
    std::memcpy(data + 1, text.data(), text.size());
    return text.size() + 1;
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
