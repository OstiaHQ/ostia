/* ostia-telemetry C ABI. See RFC-0001 §5 and RFC-0002 §9. */
#ifndef OSTIA_TELEMETRY_TELEMETRY_H
#define OSTIA_TELEMETRY_TELEMETRY_H

#include <ostia/telemetry/export.h>

#ifdef __cplusplus
extern "C" {
#endif

/* Telemetry level this library was built with: 0 off, 1 metrics, 2 trace, 3 debug. */
OSTIA_TELEMETRY_EXPORT int ostia_telemetry_build_level(void);

#ifdef __cplusplus
}
#endif

#endif
