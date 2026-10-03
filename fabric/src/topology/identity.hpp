#pragma once

#include <cstddef>
#include <nlohmann/json.hpp>
#include <string>

#include "topology/model.hpp"

namespace ostia::fabric::topology {

inline constexpr std::size_t kMaxNodes = 256;        // D12
inline constexpr std::size_t kMaxLeaves = 1'000'000; // R5: search-tree leaves per topo1 call

// "topo1:sha256:<64 hex>" (RFC-0003 §5): equal for isomorphic models once keys and data
// attributes are dropped, different otherwise. Throws TopologyError "node_cap" above kMaxNodes,
// "leaf_cap" above kMaxLeaves, "dangling_reference" or "duplicate_key" on a malformed model.
std::string topo1(const Model& model);

// RFC-0003 §7: the two node ids in sorted order plus pair.json's structural fields
// (link_class, rails), hashed as canonical JSON.
std::string pair_id(const std::string& id0, const std::string& id1, const nlohmann::json& pair);

// The certificate topo1 hashes; exposed for tests and `ostia-topo id --explain`.
std::string canonical_identity_json(const Model& model);

} // namespace ostia::fabric::topology
