// A stand-in libnvidia-ml.so.1 for the capture's end-to-end test, loaded through LD_LIBRARY_PATH
// by the real dlopen path. OSTIA_FAKE_NVML names a JSON spec that nvmlInit_v2 reads, or is
// "init_error" to make nvmlInit_v2 fail. The specs live in fabric/tests/topology/data/fake-nvml/:
// the driver and CUDA driver versions, the P2P status every GPU pair reports, and per GPU its bus
// ID in NVML's own form, name, compute capability, memory size, UUID, serial, board ID and either
// "not_supported" or a list of NVLinks (active, version, remote type, optional remote bus ID).
// Like the real library, a link query past the device's links returns
// NVML_ERROR_INVALID_ARGUMENT.
#include <cstdlib>
#include <cstring>
#include <exception>
#include <fstream>
#include <nlohmann/json.hpp>
#include <string>
#include <string_view>
#include <utility>
#include <vector>

#include "live/nvml_api.h"

// The handle type nvml_api.h leaves incomplete; its name is fixed by nvmlDevice_t.
struct nvmlDevice_st { // NOLINT(readability-identifier-naming): NVML's handle type
    struct Link {
        bool active = false;
        unsigned version = 0;
        nvmlIntNvLinkDeviceType_t type = NVML_NVLINK_DEVICE_TYPE_UNKNOWN;
        std::string remote_bus_id; // empty: the remote PCI query is not supported
    };

    std::string bus_id, name, uuid, serial;
    int cc_major = 0, cc_minor = 0;
    unsigned long long memory_bytes = 0;
    unsigned board_id = 0;
    bool nvlinks_supported = false;
    std::vector<Link> links;
};

namespace {

struct State {
    bool initialized = false;
    std::vector<nvmlDevice_st> devices;
    std::string driver;
    int cuda = 0;
    nvmlGpuP2PStatus_t p2p = NVML_P2P_STATUS_OK;
};

State& state() {
    static State s;
    return s;
}

nvmlIntNvLinkDeviceType_t link_type(std::string_view text) {
    if (text == "gpu") {
        return NVML_NVLINK_DEVICE_TYPE_GPU;
    }
    return text == "switch" ? NVML_NVLINK_DEVICE_TYPE_SWITCH : NVML_NVLINK_DEVICE_TYPE_UNKNOWN;
}

nvmlDevice_st device_of(const nlohmann::json& g) {
    nvmlDevice_st d;
    d.bus_id = g.at("bus_id").get<std::string>();
    d.name = g.at("name").get<std::string>();
    d.uuid = g.at("uuid").get<std::string>();
    d.serial = g.at("serial").get<std::string>();
    d.cc_major = g.at("cc").at(0).get<int>();
    d.cc_minor = g.at("cc").at(1).get<int>();
    d.memory_bytes = g.at("memory_bytes").get<unsigned long long>();
    d.board_id = g.at("board_id").get<unsigned>();
    const nlohmann::json& links = g.at("nvlinks");
    d.nvlinks_supported = links.is_array();
    if (d.nvlinks_supported) {
        for (const nlohmann::json& l : links) {
            d.links.push_back(
                nvmlDevice_st::Link{.active = l.at("active").get<bool>(),
                                    .version = l.at("version").get<unsigned>(),
                                    .type = link_type(l.at("remote_type").get<std::string>()),
                                    .remote_bus_id = l.value("remote_bus_id", "")});
        }
    }
    return d;
}

nvmlReturn_t load(const char* spec) {
    std::ifstream in(spec);
    if (!in) {
        return NVML_ERROR_UNKNOWN;
    }
    const nlohmann::json doc = nlohmann::json::parse(in, nullptr, false);
    if (doc.is_discarded()) {
        return NVML_ERROR_UNKNOWN;
    }
    State next;
    next.driver = doc.at("driver_version").get<std::string>();
    next.cuda = doc.at("cuda_driver_version").get<int>();
    next.p2p = static_cast<nvmlGpuP2PStatus_t>(doc.at("p2p_status").get<int>());
    for (const nlohmann::json& g : doc.at("gpus")) {
        next.devices.push_back(device_of(g));
    }
    next.initialized = true;
    state() = std::move(next);
    return NVML_SUCCESS;
}

const nvmlDevice_st* find(nvmlDevice_t device) {
    const State& s = state();
    if (!s.initialized) {
        return nullptr;
    }
    for (const nvmlDevice_st& d : s.devices) {
        if (&d == device) {
            return &d;
        }
    }
    return nullptr;
}

nvmlReturn_t copy_text(const std::string& text, char* out, unsigned length) {
    if (out == nullptr) {
        return NVML_ERROR_INVALID_ARGUMENT;
    }
    if (text.size() + 1 > length) {
        return NVML_ERROR_INSUFFICIENT_SIZE;
    }
    std::memcpy(out, text.c_str(), text.size() + 1);
    return NVML_SUCCESS;
}

// nvmlPciInfo_t carries the bus ID in NVML's own form; the capture reads only busId.
void fill_pci(const std::string& bus_id, nvmlPciInfo_t* pci) {
    *pci = nvmlPciInfo_t{};
    std::strncpy(pci->busId, bus_id.c_str(), sizeof(pci->busId) - 1);
}

// The link a query names, or the NVML error the real library gives for it.
nvmlReturn_t link_of(nvmlDevice_t device, unsigned link, const nvmlDevice_st::Link** out) {
    const nvmlDevice_st* d = find(device);
    if (d == nullptr || link >= NVML_NVLINK_MAX_LINKS) {
        return NVML_ERROR_INVALID_ARGUMENT;
    }
    if (!d->nvlinks_supported) {
        return NVML_ERROR_NOT_SUPPORTED;
    }
    if (link >= d->links.size()) {
        return NVML_ERROR_INVALID_ARGUMENT;
    }
    *out = &d->links[link];
    return NVML_SUCCESS;
}

} // namespace

// NOLINTBEGIN(readability-identifier-naming): the exported names are NVML's, resolved by dlsym.
extern "C" {

nvmlReturn_t nvmlInit_v2(void) {
    const char* spec = std::getenv("OSTIA_FAKE_NVML");
    if (spec == nullptr || std::string_view(spec) == "init_error") {
        return NVML_ERROR_DRIVER_NOT_LOADED;
    }
    // An exception must not cross the C boundary; a malformed spec is a library failure.
    try {
        return load(spec);
    } catch (const std::exception&) {
        return NVML_ERROR_UNKNOWN;
    }
}

nvmlReturn_t nvmlShutdown(void) {
    state() = State{};
    return NVML_SUCCESS;
}

nvmlReturn_t nvmlDeviceGetCount_v2(unsigned int* deviceCount) {
    if (!state().initialized) {
        return NVML_ERROR_UNINITIALIZED;
    }
    *deviceCount = static_cast<unsigned>(state().devices.size());
    return NVML_SUCCESS;
}

nvmlReturn_t nvmlDeviceGetHandleByIndex_v2(unsigned int index, nvmlDevice_t* device) {
    State& s = state();
    if (!s.initialized) {
        return NVML_ERROR_UNINITIALIZED;
    }
    if (index >= s.devices.size()) {
        return NVML_ERROR_INVALID_ARGUMENT;
    }
    *device = &s.devices[index];
    return NVML_SUCCESS;
}

nvmlReturn_t nvmlDeviceGetPciInfo_v3(nvmlDevice_t device, nvmlPciInfo_t* pci) {
    const nvmlDevice_st* d = find(device);
    if (d == nullptr) {
        return NVML_ERROR_INVALID_ARGUMENT;
    }
    fill_pci(d->bus_id, pci);
    return NVML_SUCCESS;
}

nvmlReturn_t nvmlDeviceGetName(nvmlDevice_t device, char* name, unsigned int length) {
    const nvmlDevice_st* d = find(device);
    return d == nullptr ? NVML_ERROR_INVALID_ARGUMENT : copy_text(d->name, name, length);
}

nvmlReturn_t nvmlDeviceGetCudaComputeCapability(nvmlDevice_t device, int* major, int* minor) {
    const nvmlDevice_st* d = find(device);
    if (d == nullptr) {
        return NVML_ERROR_INVALID_ARGUMENT;
    }
    *major = d->cc_major;
    *minor = d->cc_minor;
    return NVML_SUCCESS;
}

nvmlReturn_t nvmlDeviceGetMemoryInfo(nvmlDevice_t device, nvmlMemory_t* memory) {
    const nvmlDevice_st* d = find(device);
    if (d == nullptr) {
        return NVML_ERROR_INVALID_ARGUMENT;
    }
    *memory = nvmlMemory_t{.total = d->memory_bytes, .free = d->memory_bytes, .used = 0};
    return NVML_SUCCESS;
}

nvmlReturn_t nvmlDeviceGetNvLinkState(nvmlDevice_t device, unsigned int link,
                                      nvmlEnableState_t* isActive) {
    const nvmlDevice_st::Link* l = nullptr;
    const nvmlReturn_t rc = link_of(device, link, &l);
    if (rc == NVML_SUCCESS) {
        *isActive = l->active ? NVML_FEATURE_ENABLED : NVML_FEATURE_DISABLED;
    }
    return rc;
}

nvmlReturn_t nvmlDeviceGetNvLinkVersion(nvmlDevice_t device, unsigned int link,
                                        unsigned int* version) {
    const nvmlDevice_st::Link* l = nullptr;
    const nvmlReturn_t rc = link_of(device, link, &l);
    if (rc == NVML_SUCCESS) {
        *version = l->version;
    }
    return rc;
}

nvmlReturn_t nvmlDeviceGetNvLinkRemotePciInfo_v2(nvmlDevice_t device, unsigned int link,
                                                 nvmlPciInfo_t* pci) {
    const nvmlDevice_st::Link* l = nullptr;
    const nvmlReturn_t rc = link_of(device, link, &l);
    if (rc != NVML_SUCCESS) {
        return rc;
    }
    if (l->remote_bus_id.empty()) {
        return NVML_ERROR_NOT_SUPPORTED;
    }
    fill_pci(l->remote_bus_id, pci);
    return NVML_SUCCESS;
}

nvmlReturn_t nvmlDeviceGetNvLinkRemoteDeviceType(nvmlDevice_t device, unsigned int link,
                                                 nvmlIntNvLinkDeviceType_t* pNvLinkDeviceType) {
    const nvmlDevice_st::Link* l = nullptr;
    const nvmlReturn_t rc = link_of(device, link, &l);
    if (rc == NVML_SUCCESS) {
        *pNvLinkDeviceType = l->type;
    }
    return rc;
}

nvmlReturn_t nvmlDeviceGetP2PStatus(nvmlDevice_t device1, nvmlDevice_t device2,
                                    nvmlGpuP2PCapsIndex_t /*p2pIndex*/,
                                    nvmlGpuP2PStatus_t* p2pStatus) {
    if (find(device1) == nullptr || find(device2) == nullptr) {
        return NVML_ERROR_INVALID_ARGUMENT;
    }
    *p2pStatus = state().p2p;
    return NVML_SUCCESS;
}

nvmlReturn_t nvmlDeviceGetUUID(nvmlDevice_t device, char* uuid, unsigned int length) {
    const nvmlDevice_st* d = find(device);
    return d == nullptr ? NVML_ERROR_INVALID_ARGUMENT : copy_text(d->uuid, uuid, length);
}

nvmlReturn_t nvmlDeviceGetSerial(nvmlDevice_t device, char* serial, unsigned int length) {
    const nvmlDevice_st* d = find(device);
    return d == nullptr ? NVML_ERROR_INVALID_ARGUMENT : copy_text(d->serial, serial, length);
}

nvmlReturn_t nvmlDeviceGetBoardId(nvmlDevice_t device, unsigned int* boardId) {
    const nvmlDevice_st* d = find(device);
    if (d == nullptr) {
        return NVML_ERROR_INVALID_ARGUMENT;
    }
    *boardId = d->board_id;
    return NVML_SUCCESS;
}

nvmlReturn_t nvmlSystemGetDriverVersion(char* version, unsigned int length) {
    if (!state().initialized) {
        return NVML_ERROR_UNINITIALIZED;
    }
    return copy_text(state().driver, version, length);
}

nvmlReturn_t nvmlSystemGetCudaDriverVersion_v2(int* cudaDriverVersion) {
    if (!state().initialized) {
        return NVML_ERROR_UNINITIALIZED;
    }
    *cudaDriverVersion = state().cuda;
    return NVML_SUCCESS;
}

} // extern "C"
// NOLINTEND(readability-identifier-naming)
