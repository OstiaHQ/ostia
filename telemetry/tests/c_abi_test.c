/* The C ABI header compiles as C and links from a C program (RFC-0001 §5). */
#include <ostia/telemetry/config.h>
#include <ostia/telemetry/telemetry.h>

int main(void) { return ostia_telemetry_build_level() == OSTIA_TELEMETRY_LEVEL ? 0 : 1; }
