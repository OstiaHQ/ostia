#pragma once

// The part of NVML's C API the capture resolves with dlsym, declared here because the tool never
// includes nvml.h or links libnvidia-ml (RFC-0003 §1). The test-built fake libnvidia-ml.so.1
// defines these same prototypes, so a dlsym'd pointer and the function it reaches have one type
// (clang's -fsanitize=function checks that). Layouts, constants and versioned entry points follow
// the NVML API Reference Guide (docs.nvidia.com/deploy/nvml-api/), nvml.h of driver 450 and later:
// nvmlPciInfo_t is the layout nvmlDeviceGetPciInfo_v3 and nvmlDeviceGetNvLinkRemotePciInfo_v2
// fill, and nvmlMemory_t the v1 layout nvmlDeviceGetMemoryInfo fills.

// NOLINTBEGIN(readability-identifier-naming, modernize-*): NVML's own C names, typedefs, macros
// and arrays, kept as nvml.h spells them so the ABI and the documentation map one to one.
extern "C" {

typedef enum nvmlReturn_enum {
    NVML_SUCCESS = 0,
    NVML_ERROR_UNINITIALIZED = 1,
    NVML_ERROR_INVALID_ARGUMENT = 2,
    NVML_ERROR_NOT_SUPPORTED = 3,
    NVML_ERROR_NO_PERMISSION = 4,
    NVML_ERROR_NOT_FOUND = 6,
    NVML_ERROR_INSUFFICIENT_SIZE = 7,
    NVML_ERROR_DRIVER_NOT_LOADED = 9,
    NVML_ERROR_LIBRARY_NOT_FOUND = 12,
    NVML_ERROR_FUNCTION_NOT_FOUND = 13,
    NVML_ERROR_GPU_IS_LOST = 15,
    NVML_ERROR_UNKNOWN = 999
} nvmlReturn_t;

typedef struct nvmlDevice_st* nvmlDevice_t;

typedef enum nvmlEnableState_enum {
    NVML_FEATURE_DISABLED = 0,
    NVML_FEATURE_ENABLED = 1
} nvmlEnableState_t;

typedef enum nvmlIntNvLinkDeviceType_enum {
    NVML_NVLINK_DEVICE_TYPE_GPU = 0x00,
    NVML_NVLINK_DEVICE_TYPE_IBMNPU = 0x01,
    NVML_NVLINK_DEVICE_TYPE_SWITCH = 0x02,
    NVML_NVLINK_DEVICE_TYPE_UNKNOWN = 0xFF
} nvmlIntNvLinkDeviceType_t;

typedef enum nvmlGpuP2PCapsIndex_enum {
    NVML_P2P_CAPS_INDEX_READ = 0,
    NVML_P2P_CAPS_INDEX_WRITE = 1,
    NVML_P2P_CAPS_INDEX_NVLINK = 2,
    NVML_P2P_CAPS_INDEX_ATOMICS = 3,
    NVML_P2P_CAPS_INDEX_PCI = 4,
    NVML_P2P_CAPS_INDEX_UNKNOWN = 5
} nvmlGpuP2PCapsIndex_t;

typedef enum nvmlGpuP2PStatus_enum {
    NVML_P2P_STATUS_OK = 0,
    NVML_P2P_STATUS_CHIPSET_NOT_SUPPORED = 1,
    NVML_P2P_STATUS_GPU_NOT_SUPPORTED = 2,
    NVML_P2P_STATUS_IOH_TOPOLOGY_NOT_SUPPORTED = 3,
    NVML_P2P_STATUS_DISABLED_BY_REGKEY = 4,
    NVML_P2P_STATUS_NOT_SUPPORTED = 5,
    NVML_P2P_STATUS_UNKNOWN = 6
} nvmlGpuP2PStatus_t;

#define NVML_DEVICE_PCI_BUS_ID_BUFFER_SIZE 32
#define NVML_DEVICE_PCI_BUS_ID_BUFFER_V2_SIZE 16
#define NVML_DEVICE_NAME_V2_BUFFER_SIZE 96
#define NVML_DEVICE_UUID_V2_BUFFER_SIZE 96
#define NVML_DEVICE_SERIAL_BUFFER_SIZE 30
#define NVML_SYSTEM_DRIVER_VERSION_BUFFER_SIZE 80
#define NVML_NVLINK_MAX_LINKS 18

typedef struct nvmlPciInfo_st {
    char busIdLegacy[NVML_DEVICE_PCI_BUS_ID_BUFFER_V2_SIZE];
    unsigned int domain;
    unsigned int bus;
    unsigned int device;
    unsigned int pciDeviceId;
    unsigned int pciSubSystemId;
    // "00000000:3B:00.0": an eight-digit domain and upper-case hex.
    char busId[NVML_DEVICE_PCI_BUS_ID_BUFFER_SIZE];
} nvmlPciInfo_t;

typedef struct nvmlMemory_st {
    unsigned long long total; // bytes
    unsigned long long free;
    unsigned long long used;
} nvmlMemory_t;

nvmlReturn_t nvmlInit_v2(void);
nvmlReturn_t nvmlShutdown(void);
nvmlReturn_t nvmlDeviceGetCount_v2(unsigned int* deviceCount);
nvmlReturn_t nvmlDeviceGetHandleByIndex_v2(unsigned int index, nvmlDevice_t* device);
nvmlReturn_t nvmlDeviceGetPciInfo_v3(nvmlDevice_t device, nvmlPciInfo_t* pci);
nvmlReturn_t nvmlDeviceGetName(nvmlDevice_t device, char* name, unsigned int length);
nvmlReturn_t nvmlDeviceGetCudaComputeCapability(nvmlDevice_t device, int* major, int* minor);
nvmlReturn_t nvmlDeviceGetMemoryInfo(nvmlDevice_t device, nvmlMemory_t* memory);
nvmlReturn_t nvmlDeviceGetNvLinkState(nvmlDevice_t device, unsigned int link,
                                      nvmlEnableState_t* isActive);
nvmlReturn_t nvmlDeviceGetNvLinkVersion(nvmlDevice_t device, unsigned int link,
                                        unsigned int* version);
nvmlReturn_t nvmlDeviceGetNvLinkRemotePciInfo_v2(nvmlDevice_t device, unsigned int link,
                                                 nvmlPciInfo_t* pci);
nvmlReturn_t nvmlDeviceGetNvLinkRemoteDeviceType(nvmlDevice_t device, unsigned int link,
                                                 nvmlIntNvLinkDeviceType_t* pNvLinkDeviceType);
nvmlReturn_t nvmlDeviceGetP2PStatus(nvmlDevice_t device1, nvmlDevice_t device2,
                                    nvmlGpuP2PCapsIndex_t p2pIndex, nvmlGpuP2PStatus_t* p2pStatus);
nvmlReturn_t nvmlDeviceGetUUID(nvmlDevice_t device, char* uuid, unsigned int length);
nvmlReturn_t nvmlDeviceGetSerial(nvmlDevice_t device, char* serial, unsigned int length);
nvmlReturn_t nvmlDeviceGetBoardId(nvmlDevice_t device, unsigned int* boardId);
nvmlReturn_t nvmlSystemGetDriverVersion(char* version, unsigned int length);
nvmlReturn_t nvmlSystemGetCudaDriverVersion_v2(int* cudaDriverVersion);

} // extern "C"
// NOLINTEND(readability-identifier-naming, modernize-*)
