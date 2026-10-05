#pragma once

#include <cstdint>
#include <optional>
#include <string>
#include <variant>
#include <vector>

#include "core/result.hpp"

namespace ostia::fabric::topology::capture {

struct NvLink {
    int link;
    std::string state;
    int version;
    std::string remote_type;
    std::optional<std::string> remote_bus_id;
};

// NVML reported the query as unsupported, which differs from a GPU with zero links.
struct NotSupported {};

struct P2p {
    std::string a, b, read, write, nvlink, atomics;
};

struct NvmlGpu {
    std::string bus_id, name;
    int cc_major, cc_minor;
    std::int64_t memory_bytes;
    std::variant<std::vector<NvLink>, NotSupported> nvlinks;
    // Raw leak-check inputs only; they are never written to any output file.
    std::string uuid, serial, board_id;
};

struct NvmlFacts {
    std::vector<NvmlGpu> gpus;
    std::vector<P2p> p2p;
    std::string driver, cuda;
};

class NvmlApi {
  public:
    virtual ~NvmlApi() = default;
    virtual Result<NvmlFacts> query() = 0;
};

struct VerbsPort {
    std::string device, bus_id, state, link_layer, gpudirect;
    int port;
    std::variant<int, std::string> active_speed, active_width;
};

class VerbsApi {
  public:
    virtual ~VerbsApi() = default;
    // nullopt means the probe is unavailable; an empty vector means it ran and found nothing.
    virtual std::optional<std::vector<VerbsPort>> ports() = 0;
};

} // namespace ostia::fabric::topology::capture
