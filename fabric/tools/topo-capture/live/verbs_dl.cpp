#include <cstddef>
#include <cstdint>
#include <cstring>
#include <dlfcn.h>
#include <infiniband/verbs.h>
#include <memory>
#include <optional>
#include <span>
#include <string>
#include <variant>
#include <vector>

#include "core/writers.hpp"
#include "live/live.hpp"

namespace ostia::fabric::topology::capture {

namespace {

// verbs.h turns ibv_query_port into an inline wrapper; the exported symbol is the compat entry
// point, which writes only the legacy prefix of ibv_port_attr (through link_layer), so a zeroed
// whole struct is a safe buffer for any library version, and active_speed_ex (XDR) stays 0.
struct Fns {
    decltype(&ibv_get_device_list) get_list = nullptr;
    decltype(&ibv_free_device_list) free_list = nullptr;
    decltype(&ibv_open_device) open = nullptr;
    decltype(&ibv_close_device) close = nullptr;
    decltype(&ibv_query_device) query_device = nullptr;
    decltype(&ibv_query_port) query_port = nullptr;

    [[nodiscard]] bool complete() const {
        return get_list != nullptr && free_list != nullptr && open != nullptr && close != nullptr &&
               query_device != nullptr && query_port != nullptr;
    }
};

template <typename F> void resolve(void* lib, const char* name, F& fn) {
    fn = reinterpret_cast<F>(::dlsym(lib, name));
}

std::string port_state(ibv_port_state state) {
    switch (state) {
    case IBV_PORT_DOWN:
        return "DOWN";
    case IBV_PORT_INIT:
        return "INIT";
    case IBV_PORT_ARMED:
        return "ARMED";
    case IBV_PORT_ACTIVE:
        return "ACTIVE";
    case IBV_PORT_ACTIVE_DEFER:
        return "ACTIVE_DEFER";
    default:
        return "NOP";
    }
}

// An unspecified link layer is InfiniBand: providers older than RoCE never set the field.
std::string port_link_layer(std::uint8_t layer) {
    switch (layer) {
    case IBV_LINK_LAYER_UNSPECIFIED:
    case IBV_LINK_LAYER_INFINIBAND:
        return "InfiniBand";
    case IBV_LINK_LAYER_ETHERNET:
        return "Ethernet";
    default:
        return "unknown";
    }
}

class VerbsDl final : public VerbsApi {
  public:
    VerbsDl(const SysfsReader& sysfs, Diagnostics& diag) : sysfs_(sysfs), diag_(diag) {}
    VerbsDl(const VerbsDl&) = delete;
    VerbsDl& operator=(const VerbsDl&) = delete;
    VerbsDl(VerbsDl&&) = delete;
    VerbsDl& operator=(VerbsDl&&) = delete;
    ~VerbsDl() override {
        if (lib_ != nullptr) {
            ::dlclose(lib_);
        }
    }

    // RFC-0003 §2.3: "ok" exactly when the library loads and lists devices, zero included.
    std::optional<std::vector<VerbsPort>> ports() override {
        if (lib_ == nullptr) {
            lib_ = ::dlopen("libibverbs.so.1", RTLD_NOW | RTLD_LOCAL);
        }
        if (lib_ == nullptr) {
            diag_.add("verbs: libibverbs.so.1 could not be loaded");
            return std::nullopt;
        }
        Fns fns;
        resolve(lib_, "ibv_get_device_list", fns.get_list);
        resolve(lib_, "ibv_free_device_list", fns.free_list);
        resolve(lib_, "ibv_open_device", fns.open);
        resolve(lib_, "ibv_close_device", fns.close);
        resolve(lib_, "ibv_query_device", fns.query_device);
        resolve(lib_, "ibv_query_port", fns.query_port);
        if (!fns.complete()) {
            diag_.add("verbs: libibverbs.so.1 lacks a required entry point");
            return std::nullopt;
        }
        int count = 0;
        ibv_device** list = fns.get_list(&count);
        if (list == nullptr) {
            diag_.add("verbs: ibv_get_device_list failed");
            return std::nullopt;
        }
        const std::unique_ptr<ibv_device*, decltype(fns.free_list)> owned(list, fns.free_list);

        const std::string gpudirect = gpudirect_state();
        std::vector<VerbsPort> out;
        std::size_t no_pci = 0;
        std::size_t unreadable = 0;
        for (ibv_device* dev : std::span(list, static_cast<std::size_t>(count))) {
            const std::string name(dev->name, ::strnlen(dev->name, sizeof(dev->name)));
            // rxe and siw have no PCI parent; their ports would join nothing in the model.
            const std::optional<std::string> parent =
                sysfs_.link_name("class/infiniband/" + name + "/device");
            const Result<std::string> bus =
                parent ? normalize_bus_id(*parent) : Result<std::string>::failure("no_parent");
            if (!bus.ok()) {
                ++no_pci;
                continue;
            }
            if (!add_ports(fns, dev, name, bus.value(), gpudirect, out)) {
                ++unreadable;
            }
        }
        if (no_pci > 0) {
            diag_.add("verbs: skipped " + std::to_string(no_pci) +
                      " device(s) without a PCI bus ID");
        }
        if (unreadable > 0) {
            diag_.add("verbs: skipped " + std::to_string(unreadable) +
                      " device(s) that could not be opened or queried");
        }
        return out;
    }

  private:
    // False when the device cannot be opened or queried; its ports are then left out whole, so
    // a device is never half-listed.
    static bool add_ports(const Fns& fns, ibv_device* dev, const std::string& name,
                          const std::string& bus, const std::string& gpudirect,
                          std::vector<VerbsPort>& out) {
        const std::unique_ptr<ibv_context, decltype(fns.close)> ctx(fns.open(dev), fns.close);
        if (ctx == nullptr) {
            return false;
        }
        ibv_device_attr attr{};
        if (fns.query_device(ctx.get(), &attr) != 0) {
            return false;
        }
        std::vector<VerbsPort> ports;
        for (int p = 1; p <= attr.phys_port_cnt; ++p) {
            ibv_port_attr port{};
            if (fns.query_port(ctx.get(), static_cast<std::uint8_t>(p),
                               reinterpret_cast<_compat_ibv_port_attr*>(&port)) != 0) {
                return false;
            }
            ports.push_back(VerbsPort{.device = name,
                                      .bus_id = bus,
                                      .state = port_state(port.state),
                                      .link_layer = port_link_layer(port.link_layer),
                                      .gpudirect = gpudirect,
                                      .port = p,
                                      .active_speed = static_cast<int>(port.active_speed),
                                      .active_width = static_cast<int>(port.active_width)});
        }
        out.insert(out.end(), ports.begin(), ports.end());
        return true;
    }

    // nvidia_peermem is the RDMA peer-memory module; without it, dma-buf registration needs both
    // ibv_reg_dmabuf_mr and the NVIDIA kernel module, which is the most a probe can show without
    // registering GPU memory.
    [[nodiscard]] std::string gpudirect_state() const {
        if (!sysfs_.list("module/nvidia_peermem").empty()) {
            return "nvidia_peermem";
        }
        if (::dlsym(lib_, "ibv_reg_dmabuf_mr") != nullptr &&
            !sysfs_.list("module/nvidia").empty()) {
            return "dmabuf";
        }
        return "none";
    }

    const SysfsReader& sysfs_;
    Diagnostics& diag_;
    void* lib_ = nullptr;
};

} // namespace

std::unique_ptr<VerbsApi> make_verbs(const SysfsReader& sysfs, Diagnostics& diag) {
    return std::make_unique<VerbsDl>(sysfs, diag);
}

} // namespace ostia::fabric::topology::capture
