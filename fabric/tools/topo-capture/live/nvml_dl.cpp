#include <algorithm>
#include <array>
#include <cstddef>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <dlfcn.h>
#include <memory>
#include <optional>
#include <string>
#include <utility>
#include <variant>
#include <vector>

#include "core/writers.hpp"
#include "live/live.hpp"
#include "live/nvml_api.h"

namespace ostia::fabric::topology::capture {

namespace {

constexpr unsigned kNvmlMaxLinks = NVML_NVLINK_MAX_LINKS;
constexpr int kCudaMajorScale = 1000; // nvmlSystemGetCudaDriverVersion_v2 gives 12040 for 12.4
constexpr int kCudaMinorScale = 10;

// Every entry point, resolved once; a null pointer is a symbol this driver does not export.
struct Fns {
    decltype(&nvmlInit_v2) init = nullptr;
    decltype(&nvmlShutdown) shutdown = nullptr;
    decltype(&nvmlDeviceGetCount_v2) count = nullptr;
    decltype(&nvmlDeviceGetHandleByIndex_v2) handle = nullptr;
    decltype(&nvmlDeviceGetPciInfo_v3) pci = nullptr;
    decltype(&nvmlDeviceGetName) name = nullptr;
    decltype(&nvmlDeviceGetCudaComputeCapability) cc = nullptr;
    decltype(&nvmlDeviceGetMemoryInfo) memory = nullptr;
    decltype(&nvmlDeviceGetNvLinkState) link_state = nullptr;
    decltype(&nvmlDeviceGetNvLinkVersion) link_version = nullptr;
    decltype(&nvmlDeviceGetNvLinkRemotePciInfo_v2) link_pci = nullptr;
    decltype(&nvmlDeviceGetNvLinkRemoteDeviceType) link_type = nullptr;
    decltype(&nvmlDeviceGetP2PStatus) p2p = nullptr;
    decltype(&nvmlDeviceGetUUID) uuid = nullptr;
    decltype(&nvmlDeviceGetSerial) serial = nullptr;
    decltype(&nvmlDeviceGetBoardId) board = nullptr;
    decltype(&nvmlSystemGetDriverVersion) driver = nullptr;
    decltype(&nvmlSystemGetCudaDriverVersion_v2) cuda = nullptr;

    [[nodiscard]] bool has_required() const {
        return init != nullptr && shutdown != nullptr && count != nullptr && handle != nullptr &&
               pci != nullptr;
    }
};

template <typename F> void resolve(void* lib, const char* name, F& fn) {
    fn = reinterpret_cast<F>(::dlsym(lib, name));
}

// NVML terminates its strings, but a short or misbehaving driver must not make us read past the
// buffer.
template <std::size_t N> std::string text_of(const std::array<char, N>& buf) {
    return {buf.data(), ::strnlen(buf.data(), N)};
}

template <std::size_t N> unsigned size_of(const std::array<char, N>& /*buf*/) {
    return static_cast<unsigned>(N);
}

// nvmlShutdown pairs with every successful nvmlInit_v2, whichever way query() returns.
class Session {
  public:
    explicit Session(decltype(&nvmlShutdown) shutdown) : shutdown_(shutdown) {}
    Session(const Session&) = delete;
    Session& operator=(const Session&) = delete;
    Session(Session&&) = delete;
    Session& operator=(Session&&) = delete;
    ~Session() { shutdown_(); }

  private:
    decltype(&nvmlShutdown) shutdown_;
};

std::string p2p_value(nvmlReturn_t rc, nvmlGpuP2PStatus_t status) {
    if (rc == NVML_ERROR_NOT_SUPPORTED) {
        return "not_supported";
    }
    if (rc != NVML_SUCCESS || status == NVML_P2P_STATUS_UNKNOWN) {
        return "unknown";
    }
    return status == NVML_P2P_STATUS_OK ? "ok" : "not_supported";
}

std::string remote_type(nvmlIntNvLinkDeviceType_t type) {
    switch (type) {
    case NVML_NVLINK_DEVICE_TYPE_GPU:
        return "gpu";
    case NVML_NVLINK_DEVICE_TYPE_SWITCH:
        return "switch";
    default:
        return "unknown";
    }
}

class NvmlDl final : public NvmlApi {
  public:
    NvmlDl(void* lib, Diagnostics& diag) : lib_(lib), diag_(diag) {
        resolve(lib_, "nvmlInit_v2", fns_.init);
        resolve(lib_, "nvmlShutdown", fns_.shutdown);
        resolve(lib_, "nvmlDeviceGetCount_v2", fns_.count);
        resolve(lib_, "nvmlDeviceGetHandleByIndex_v2", fns_.handle);
        resolve(lib_, "nvmlDeviceGetPciInfo_v3", fns_.pci);
        resolve(lib_, "nvmlDeviceGetName", fns_.name);
        resolve(lib_, "nvmlDeviceGetCudaComputeCapability", fns_.cc);
        resolve(lib_, "nvmlDeviceGetMemoryInfo", fns_.memory);
        resolve(lib_, "nvmlDeviceGetNvLinkState", fns_.link_state);
        resolve(lib_, "nvmlDeviceGetNvLinkVersion", fns_.link_version);
        resolve(lib_, "nvmlDeviceGetNvLinkRemotePciInfo_v2", fns_.link_pci);
        resolve(lib_, "nvmlDeviceGetNvLinkRemoteDeviceType", fns_.link_type);
        resolve(lib_, "nvmlDeviceGetP2PStatus", fns_.p2p);
        resolve(lib_, "nvmlDeviceGetUUID", fns_.uuid);
        resolve(lib_, "nvmlDeviceGetSerial", fns_.serial);
        resolve(lib_, "nvmlDeviceGetBoardId", fns_.board);
        resolve(lib_, "nvmlSystemGetDriverVersion", fns_.driver);
        resolve(lib_, "nvmlSystemGetCudaDriverVersion_v2", fns_.cuda);
    }
    NvmlDl(const NvmlDl&) = delete;
    NvmlDl& operator=(const NvmlDl&) = delete;
    NvmlDl(NvmlDl&&) = delete;
    NvmlDl& operator=(NvmlDl&&) = delete;
    ~NvmlDl() override { ::dlclose(lib_); }

    Result<NvmlFacts> query() override {
        if (!fns_.has_required()) {
            diag_.add("nvml: libnvidia-ml.so.1 lacks a required entry point");
            return Result<NvmlFacts>::failure("nvml_symbols");
        }
        unreadable_links_ = 0;
        dropped_remotes_ = 0;
        if (fns_.init() != NVML_SUCCESS) {
            return Result<NvmlFacts>::failure("nvml_init");
        }
        const Session session(fns_.shutdown);
        unsigned count = 0;
        if (fns_.count(&count) != NVML_SUCCESS) {
            return Result<NvmlFacts>::failure("nvml_device_count");
        }

        NvmlFacts facts;
        std::vector<std::pair<std::string, nvmlDevice_t>> devices; // bus ID, handle
        for (unsigned i = 0; i < count; ++i) {
            nvmlDevice_t dev = nullptr;
            if (fns_.handle(i, &dev) != NVML_SUCCESS) {
                return Result<NvmlFacts>::failure("nvml_device_handle");
            }
            nvmlPciInfo_t pci{};
            if (fns_.pci(dev, &pci) != NVML_SUCCESS) {
                return Result<NvmlFacts>::failure("nvml_pci_info");
            }
            // A GPU without a usable bus ID cannot be joined to hwloc or ranked for cuda_ordinal,
            // so the whole file is withheld rather than written with a gap.
            Result<std::string> bus = bus_id(pci);
            if (!bus.ok()) {
                diag_.add("nvml: a GPU bus ID cannot be written (" + bus.error() + ")");
                return Result<NvmlFacts>::failure(bus.error());
            }
            facts.gpus.push_back(gpu(dev, bus.value()));
            devices.emplace_back(bus.value(), dev);
        }

        std::ranges::sort(devices, {}, &std::pair<std::string, nvmlDevice_t>::first);
        for (std::size_t a = 0; a < devices.size(); ++a) {
            for (std::size_t b = a + 1; b < devices.size(); ++b) {
                facts.p2p.push_back(p2p(devices[a], devices[b]));
            }
        }
        facts.driver = driver_version();
        facts.cuda = cuda_version();
        if (unreadable_links_ > 0) {
            diag_.add("nvml: " + std::to_string(unreadable_links_) +
                      " NVLink state query(ies) failed; those links are recorded inactive");
        }
        if (dropped_remotes_ > 0) {
            diag_.add("nvml: " + std::to_string(dropped_remotes_) +
                      " NVLink remote bus ID(s) cannot be written and are omitted");
        }
        return facts;
    }

  private:
    static Result<std::string> bus_id(const nvmlPciInfo_t& pci) {
        const std::array<char, NVML_DEVICE_PCI_BUS_ID_BUFFER_SIZE> raw = std::to_array(pci.busId);
        return normalize_bus_id(text_of(raw));
    }

    NvmlGpu gpu(nvmlDevice_t dev, const std::string& bus) {
        NvmlGpu g{.bus_id = bus,
                  .name = "unknown",
                  .cc_major = 0,
                  .cc_minor = 0,
                  .memory_bytes = 0,
                  .nvlinks = NotSupported{},
                  .uuid = {},
                  .serial = {},
                  .board_id = {}};
        std::array<char, NVML_DEVICE_NAME_V2_BUFFER_SIZE> name{};
        if (fns_.name != nullptr && fns_.name(dev, name.data(), size_of(name)) == NVML_SUCCESS) {
            g.name = text_of(name);
        }
        if (fns_.cc != nullptr) {
            int major = 0;
            int minor = 0;
            if (fns_.cc(dev, &major, &minor) == NVML_SUCCESS) {
                g.cc_major = major;
                g.cc_minor = minor;
            }
        }
        nvmlMemory_t memory{};
        if (fns_.memory != nullptr && fns_.memory(dev, &memory) == NVML_SUCCESS) {
            g.memory_bytes = static_cast<std::int64_t>(memory.total);
        }
        g.nvlinks = nvlinks(dev);

        // Leak-check inputs only (RFC-0003 §3); never written.
        std::array<char, NVML_DEVICE_UUID_V2_BUFFER_SIZE> uuid{};
        if (fns_.uuid != nullptr && fns_.uuid(dev, uuid.data(), size_of(uuid)) == NVML_SUCCESS) {
            g.uuid = text_of(uuid);
        }
        std::array<char, NVML_DEVICE_SERIAL_BUFFER_SIZE> serial{};
        if (fns_.serial != nullptr &&
            fns_.serial(dev, serial.data(), size_of(serial)) == NVML_SUCCESS) {
            g.serial = text_of(serial);
        }
        unsigned board = 0;
        if (fns_.board != nullptr && fns_.board(dev, &board) == NVML_SUCCESS) {
            // nvidia-smi prints the board ID in hex, the spelling most likely to be pasted.
            std::array<char, 16> buf{};
            std::snprintf(buf.data(), buf.size(), "0x%x", board);
            g.board_id = buf.data();
        }
        return g;
    }

    // NVML has no link count: links are probed up to NVML_NVLINK_MAX_LINKS and
    // NVML_ERROR_INVALID_ARGUMENT marks the end. NOT_SUPPORTED on link 0 means the GPU cannot
    // report NVLinks at all (RFC-0003 §2.2), which differs from a GPU with none.
    std::variant<std::vector<NvLink>, NotSupported> nvlinks(nvmlDevice_t dev) {
        if (fns_.link_state == nullptr) {
            return NotSupported{};
        }
        std::vector<NvLink> links;
        for (unsigned l = 0; l < kNvmlMaxLinks; ++l) {
            nvmlEnableState_t active = NVML_FEATURE_DISABLED;
            const nvmlReturn_t rc = fns_.link_state(dev, l, &active);
            if (rc == NVML_ERROR_INVALID_ARGUMENT) {
                break;
            }
            if (rc == NVML_ERROR_NOT_SUPPORTED && l == 0) {
                return NotSupported{};
            }
            NvLink link{.link = static_cast<int>(l),
                        .state = "inactive",
                        .version = 0,
                        .remote_type = "unknown",
                        .remote_bus_id = std::nullopt};
            if (rc == NVML_ERROR_NOT_SUPPORTED) {
                link.state = "not_supported";
            } else if (rc != NVML_SUCCESS) {
                ++unreadable_links_;
            } else if (active == NVML_FEATURE_ENABLED) {
                link.state = "active";
            }
            unsigned version = 0;
            if (fns_.link_version != nullptr &&
                fns_.link_version(dev, l, &version) == NVML_SUCCESS) {
                link.version = static_cast<int>(version);
            }
            nvmlIntNvLinkDeviceType_t type = NVML_NVLINK_DEVICE_TYPE_UNKNOWN;
            if (fns_.link_type != nullptr && fns_.link_type(dev, l, &type) == NVML_SUCCESS) {
                link.remote_type = remote_type(type);
            }
            nvmlPciInfo_t pci{};
            if (link.remote_type != "unknown" && fns_.link_pci != nullptr &&
                fns_.link_pci(dev, l, &pci) == NVML_SUCCESS) {
                Result<std::string> remote = bus_id(pci);
                if (remote.ok()) {
                    link.remote_bus_id = remote.value();
                } else {
                    ++dropped_remotes_;
                }
            }
            links.push_back(std::move(link));
        }
        return links;
    }

    [[nodiscard]] P2p p2p(const std::pair<std::string, nvmlDevice_t>& a,
                          const std::pair<std::string, nvmlDevice_t>& b) const {
        const auto status = [&](nvmlGpuP2PCapsIndex_t index) {
            if (fns_.p2p == nullptr) {
                return std::string("unknown");
            }
            nvmlGpuP2PStatus_t value = NVML_P2P_STATUS_UNKNOWN;
            const nvmlReturn_t rc = fns_.p2p(a.second, b.second, index, &value);
            return p2p_value(rc, value);
        };
        return P2p{.a = a.first,
                   .b = b.first,
                   .read = status(NVML_P2P_CAPS_INDEX_READ),
                   .write = status(NVML_P2P_CAPS_INDEX_WRITE),
                   .nvlink = status(NVML_P2P_CAPS_INDEX_NVLINK),
                   .atomics = status(NVML_P2P_CAPS_INDEX_ATOMICS)};
    }

    // Empty when unknown; meta_json writes "unknown" for it.
    [[nodiscard]] std::string driver_version() const {
        std::array<char, NVML_SYSTEM_DRIVER_VERSION_BUFFER_SIZE> buf{};
        if (fns_.driver == nullptr || fns_.driver(buf.data(), size_of(buf)) != NVML_SUCCESS) {
            return {};
        }
        return text_of(buf);
    }

    [[nodiscard]] std::string cuda_version() const {
        int version = 0;
        if (fns_.cuda == nullptr || fns_.cuda(&version) != NVML_SUCCESS || version <= 0) {
            return {};
        }
        std::string out = std::to_string(version / kCudaMajorScale);
        out += '.';
        out += std::to_string((version % kCudaMajorScale) / kCudaMinorScale);
        return out;
    }

    void* lib_;
    Diagnostics& diag_;
    Fns fns_;
    std::size_t unreadable_links_ = 0;
    std::size_t dropped_remotes_ = 0;
};

} // namespace

std::unique_ptr<NvmlApi> load_nvml(Diagnostics& diag) {
    // The versioned soname is what the driver installs; libnvidia-ml.so exists only with the
    // development package.
    void* lib = ::dlopen("libnvidia-ml.so.1", RTLD_NOW | RTLD_LOCAL);
    if (lib == nullptr) {
        diag.add("nvml: libnvidia-ml.so.1 could not be loaded");
        return nullptr;
    }
    return std::make_unique<NvmlDl>(lib, diag);
}

} // namespace ostia::fabric::topology::capture
