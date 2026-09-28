/* ostia-fabric C ABI. The fabric API itself comes with the ostia-fabric RFC. */
#ifndef OSTIA_FABRIC_FABRIC_H
#define OSTIA_FABRIC_FABRIC_H

#include <ostia/fabric/export.h>

#ifdef __cplusplus
extern "C" {
#endif

/* Telemetry level of the libostia-telemetry this library is linked with (RFC-0001 §5). */
OSTIA_FABRIC_EXPORT int ostia_fabric_telemetry_build_level(void);

#ifdef __cplusplus
}
#endif

#endif
