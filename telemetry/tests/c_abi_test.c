/* The C ABI header compiles as C and links from a C program (RFC-0001 §5). */
#include <ostia/telemetry/telemetry.h>

int main(void) { return ostia_telemetry_build_level() == 1 ? 0 : 1; }
