#include <ostia/fabric/fabric.h>

// The installed stack reports the flavour its package config records (RFC-0001 §5).
int main() { return ostia_fabric_telemetry_build_level() == OSTIA_EXPECT_LEVEL ? 0 : 1; }
