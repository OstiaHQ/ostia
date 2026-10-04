#pragma once

#include <cstddef>
#include <filesystem>
#include <map>
#include <set>
#include <string>
#include <string_view>
#include <utility>
#include <vector>

#include "core/apis.hpp"
#include "core/diagnostics.hpp"
#include "core/sysfs.hpp"

namespace ostia::fabric::topology::capture {

enum class IdKind {
    mac,
    guid,
    gid,
    ipv4,
    ipv6,
    ipoib,
    uuid,
    serial,
    hostname,
    machine_id,
    dmi,
    instance_id,
    extra,
    // Not an identifier: a file the search could not read, which cannot be shown clean.
    unreadable
};

std::string_view to_string(IdKind kind);

// RFC-0003 §3. Hex identifiers match case- and separator-insensitively: the needle and every
// output line are lowercased with ":-._" and spaces removed. Everything else matches as tokens,
// a token being a maximal run of [A-Za-z0-9._-]: a single-token value must equal a whole token,
// a multi-token value is a substring anchored at token boundaries.
enum class MatchMode { hex, token };

struct RawEntry {
    IdKind kind;
    MatchMode mode;
    // Normalised for its mode, so equal identifiers spelled differently are stored once.
    std::string needle;
};

// The raw identifiers of the source machine. The values never leave this object: findings,
// counts and diagnostics carry kinds only.
class RawSet {
  public:
    // The mode follows the kind: MACs, GUIDs, GIDs, IPoIB addresses, UUIDs and machine IDs are
    // hex, the rest tokens. Values under 6 normalised characters are skipped and counted, and
    // all-zero or all-f hex values and loopback or unspecified IPs are dropped.
    void add(IdKind kind, std::string value);
    void add(IdKind kind, std::string value, MatchMode mode);

    [[nodiscard]] std::size_t skipped_short() const { return skipped_short_; }
    [[nodiscard]] std::map<IdKind, std::size_t> counts() const;
    [[nodiscard]] const std::vector<RawEntry>& entries() const { return entries_; }

  private:
    std::vector<RawEntry> entries_;
    std::set<std::pair<MatchMode, std::string>> seen_;
    std::size_t skipped_short_ = 0;
};

// Sources are read independently of what the capture wrote (RFC-0003 §3 step 1): rooted sysfs
// net addresses, InfiniBand GUIDs and GIDs, NIC VPD (SN and V0-VZ), the identifying DMI fields
// (*_serial, *_uuid, *_asset_tag and instance-ID-shaped values; never product_name or
// sys_vendor), NVML UUIDs, serials and board IDs, and the extra identifiers. live_host adds
// getifaddrs, the hostname and the FQDN built from it (see add_host_names), and /etc/machine-id,
// which read the real machine whatever the sysfs root. No source performs a DNS or network
// lookup. Each source's count and wall time go to diag.
RawSet collect_raw(const SysfsReader& sysfs, const NvmlFacts* nvml,
                   const std::vector<std::string>& extra, Diagnostics& diag, bool live_host);

// The hostname, its first label, and the FQDN: the hostname itself when it contains a dot, and
// hostname + "." + the domain read from domainname_file (/proc/sys/kernel/domainname on a live
// host), which is skipped when absent, empty or "(none)". The file is a parameter so tests can
// supply one.
void add_host_names(RawSet& raw, const std::string& hostname,
                    const std::filesystem::path& domainname_file);

// RFC-0003 §3 step 2: EUI-64 link-local GIDs from MACs (ff:fe inserted, universal/local bit
// flipped) and GUIDs, IPv4-mapped GIDs (both ways: an IPv4 gives its mapped GID, and a mapped
// GID gives its IPv4 with that IPv4's own derived forms), the GID and GUID inside a 20-byte IPoIB
// address, ip-a-b-c-d hostnames, the full and compressed spellings of every IPv6 address or GID,
// bare GPU/MIG UUIDs and instance IDs embedded in DMI values. Derived forms keep their source's
// kind.
void expand_derived(RawSet& raw);

// locator is a JSON pointer for .json files and an element path with "/@attribute" for .xml
// files, empty otherwise, and is withheld when it would itself contain an identifier.
struct Finding {
    std::string file; // file name only: the output directory may be named after the host
    int line;
    std::string locator;
    IdKind kind;
};

// Indexed: each line is hashed by sliding windows of every distinct needle length and split into
// tokens, so the cost grows with the output size times the number of distinct lengths, not with
// the number of identifiers. A file that cannot be read is reported with line 0 and kind
// unreadable, so a caller can tell it from a leak.
std::vector<Finding> search(const RawSet& raw, const std::vector<std::filesystem::path>& files);

// One identifier per line; surrounding whitespace is trimmed, and blank lines and lines
// starting with '#' are ignored. A missing file gives an empty list.
std::vector<std::string> read_extra_identifiers(const std::filesystem::path& path);

// "file:line locator: kind", never the value; "file:0 unreadable" for an unreadable file.
std::string to_string(const Finding& finding);

// The identifiers in a PCI VPD image (PCI Local Bus 3.0 §I): SN and V0-VZ from the read-only
// and read-write sections. Parsing stops at the end tag or the end of the bytes.
std::vector<std::string> vpd_identifiers(std::string_view vpd);

} // namespace ostia::fabric::topology::capture
