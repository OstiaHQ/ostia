#include "core/leak.hpp"

#include <algorithm>
#include <arpa/inet.h>
#include <array>
#include <cctype>
#include <charconv>
#include <chrono>
#include <cstdint>
#include <cstdio>
#include <fstream>
#include <ifaddrs.h>
#include <initializer_list>
#include <iterator>
#include <map>
#include <netinet/in.h>
#include <optional>
#include <set>
#include <span>
#include <string>
#include <sys/socket.h>
#include <system_error>
#include <tuple>
#include <unistd.h>
#include <unordered_map>
#include <utility>
#include <vector>
#ifdef __linux__
#include <netpacket/packet.h>
#endif

namespace ostia::fabric::topology::capture {

namespace {

namespace fs = std::filesystem;

constexpr std::size_t kMinNeedle = 6;
// Real VPD images are a few hundred bytes; the cap bounds a slow or malformed read.
constexpr std::size_t kVpdCap = 4096;
constexpr std::string_view kPciDevices = "bus/pci/devices/";
constexpr std::string_view kWithheld = "(withheld)";

using Ip6 = std::array<std::uint8_t, 16>;

bool is_separator(char c) { return c == ':' || c == '-' || c == '.' || c == '_' || c == ' '; }

bool is_token_char(char c) {
    return std::isalnum(static_cast<unsigned char>(c)) != 0 || c == '.' || c == '_' || c == '-';
}

bool is_hex_digit(char c) { return std::isxdigit(static_cast<unsigned char>(c)) != 0; }

char lower(char c) { return static_cast<char>(std::tolower(static_cast<unsigned char>(c))); }

std::string lowered(std::string_view text) {
    std::string out;
    out.reserve(text.size());
    for (const char c : text) {
        out += lower(c);
    }
    return out;
}

std::string normalised_hex(std::string_view text) {
    std::string out;
    out.reserve(text.size());
    for (const char c : text) {
        if (!is_separator(c)) {
            out += lower(c);
        }
    }
    return out;
}

std::string trimmed(std::string_view text) {
    constexpr std::string_view kBlank{" \t\r\n\0", 5};
    const std::size_t first = text.find_first_not_of(kBlank);
    if (first == std::string_view::npos) {
        return {};
    }
    const std::size_t last = text.find_last_not_of(kBlank);
    return std::string(text.substr(first, last - first + 1));
}

bool all_tokens_chars(std::string_view text) { return std::ranges::all_of(text, is_token_char); }

std::optional<std::vector<std::uint8_t>> hex_bytes(std::string_view hex) {
    if (hex.empty() || hex.size() % 2 != 0) {
        return std::nullopt;
    }
    std::vector<std::uint8_t> out;
    out.reserve(hex.size() / 2);
    for (std::size_t i = 0; i < hex.size(); i += 2) {
        unsigned value = 0;
        const char* const first = hex.data() + i;
        const auto [ptr, ec] = std::from_chars(first, first + 2, value, 16);
        if (ec != std::errc{} || ptr != first + 2) {
            return std::nullopt;
        }
        out.push_back(static_cast<std::uint8_t>(value));
    }
    return out;
}

std::string hex_of(std::span<const std::uint8_t> bytes) {
    constexpr std::string_view kDigits = "0123456789abcdef";
    std::string out;
    out.reserve(bytes.size() * 2);
    for (const std::uint8_t b : bytes) {
        out += kDigits[b >> 4U];
        out += kDigits[b & 0x0fU];
    }
    return out;
}

// "c000:211": 16-bit groups without leading zeros.
std::string hex_groups(std::span<const std::uint8_t> bytes) {
    std::string out;
    for (std::size_t i = 0; i + 1 < bytes.size(); i += 2) {
        if (!out.empty()) {
            out += ':';
        }
        const std::string group = hex_of(bytes.subspan(i, 2));
        const std::size_t first = group.find_first_not_of('0');
        out += first == std::string::npos ? std::string("0") : group.substr(first);
    }
    return out;
}

std::optional<Ip6> parse_ipv6(const std::string& text) {
    Ip6 bytes{};
    if (inet_pton(AF_INET6, text.c_str(), bytes.data()) != 1) {
        return std::nullopt;
    }
    return bytes;
}

std::optional<std::array<std::uint8_t, 4>> parse_ipv4(const std::string& text) {
    std::array<std::uint8_t, 4> bytes{};
    if (inet_pton(AF_INET, text.c_str(), bytes.data()) != 1) {
        return std::nullopt;
    }
    return bytes;
}

std::string ipv6_text(const Ip6& bytes) {
    std::array<char, INET6_ADDRSTRLEN> buf{};
    if (inet_ntop(AF_INET6, bytes.data(), buf.data(), static_cast<socklen_t>(buf.size())) ==
        nullptr) {
        return {};
    }
    return buf.data();
}

bool is_dropped_ipv4(const std::array<std::uint8_t, 4>& b) {
    constexpr std::uint8_t kLoopbackNet = 127;
    return b[0] == kLoopbackNet || std::ranges::all_of(b, [](std::uint8_t x) { return x == 0; });
}

bool is_dropped_ipv6(const Ip6& b) {
    const bool zero_prefix =
        std::all_of(b.begin(), b.end() - 1, [](std::uint8_t x) { return x == 0; });
    // :: and ::1.
    if (zero_prefix && b[15] <= 1) {
        return true;
    }
    // ::ffff:127.0.0.0/8, the v4-mapped loopback.
    const bool mapped =
        std::all_of(b.begin(), b.begin() + 10, [](std::uint8_t x) { return x == 0; }) &&
        b[10] == 0xff && b[11] == 0xff;
    return mapped && b[12] == 127;
}

// Value-dropping rules that need the original spelling, before normalisation loses it.
bool is_dropped(IdKind kind, const std::string& value) {
    if (kind == IdKind::ipv4) {
        const auto v4 = parse_ipv4(value);
        return v4 && is_dropped_ipv4(*v4);
    }
    if (kind == IdKind::ipv6) {
        const auto v6 = parse_ipv6(value);
        return v6 && is_dropped_ipv6(*v6);
    }
    return false;
}

MatchMode default_mode(IdKind kind) {
    switch (kind) {
    case IdKind::mac:
    case IdKind::guid:
    case IdKind::gid:
    case IdKind::ipoib:
    case IdKind::uuid:
    case IdKind::machine_id:
        return MatchMode::hex;
    default:
        return MatchMode::token;
    }
}

void add_ipv6_forms(RawSet& raw, IdKind kind, const Ip6& bytes) {
    raw.add(kind, hex_of(bytes), MatchMode::hex);
    raw.add(kind, ipv6_text(bytes), MatchMode::token);
}

Ip6 link_local(std::span<const std::uint8_t, 8> interface_id) {
    Ip6 gid{};
    gid[0] = 0xfe;
    gid[1] = 0x80;
    std::ranges::copy(interface_id, gid.begin() + 8);
    return gid;
}

// i-[0-9a-f]{8,17} standing alone or inside a longer value, as EC2 writes it to DMI.
std::vector<std::string> instance_ids(std::string_view text) {
    constexpr std::size_t kMinDigits = 8;
    constexpr std::size_t kMaxDigits = 17;
    std::vector<std::string> out;
    for (std::size_t i = 0; i + 1 < text.size(); ++i) {
        if (text[i] != 'i' || text[i + 1] != '-') {
            continue;
        }
        if (i > 0 && std::isalnum(static_cast<unsigned char>(text[i - 1])) != 0) {
            continue;
        }
        std::size_t end = i + 2;
        while (end < text.size() && is_hex_digit(text[end])) {
            ++end;
        }
        const std::size_t digits = end - i - 2;
        const bool bounded =
            end == text.size() || std::isalnum(static_cast<unsigned char>(text[end])) == 0;
        if (digits >= kMinDigits && digits <= kMaxDigits && bounded) {
            out.emplace_back(text.substr(i, end - i));
        }
    }
    return out;
}

void expand_token(RawSet& raw, const RawEntry& entry);

// A ::ffff:a.b.c.d GID carries an IPv4 address, which leaks in its own spellings too.
void add_mapped_ipv4(RawSet& raw, IdKind kind, const Ip6& gid) {
    const bool mapped =
        std::all_of(gid.begin(), gid.begin() + 10, [](std::uint8_t x) { return x == 0; }) &&
        gid[10] == 0xff && gid[11] == 0xff;
    if (!mapped) {
        return;
    }
    std::string dotted;
    for (std::size_t i = 12; i < gid.size(); ++i) {
        if (!dotted.empty()) {
            dotted += '.';
        }
        dotted += std::to_string(gid[i]);
    }
    raw.add(kind, dotted, MatchMode::token);
    expand_token(raw, RawEntry{.kind = kind, .mode = MatchMode::token, .needle = dotted});
}

void expand_hex(RawSet& raw, const RawEntry& entry) {
    constexpr std::size_t kMacBytes = 6;
    constexpr std::size_t kGuidBytes = 8;
    constexpr std::size_t kGidBytes = 16;
    constexpr std::size_t kIpoibBytes = 20;
    if (entry.kind == IdKind::uuid &&
        (entry.needle.starts_with("gpu") || entry.needle.starts_with("mig"))) {
        raw.add(entry.kind, entry.needle.substr(3), MatchMode::hex);
        return;
    }
    const auto bytes = hex_bytes(entry.needle);
    if (!bytes) {
        return;
    }
    const std::span<const std::uint8_t> b(*bytes);
    if (b.size() == kMacBytes) {
        // EUI-64 (RFC 4291 appendix A): ff:fe in the middle, universal/local bit flipped.
        constexpr std::uint8_t kUniversalLocal = 0x02;
        const std::array<std::uint8_t, 8> eui{static_cast<std::uint8_t>(b[0] ^ kUniversalLocal),
                                              b[1],
                                              b[2],
                                              0xff,
                                              0xfe,
                                              b[3],
                                              b[4],
                                              b[5]};
        raw.add(entry.kind, hex_of(eui), MatchMode::hex);
        add_ipv6_forms(raw, entry.kind, link_local(eui));
    } else if (b.size() == kGuidBytes) {
        add_ipv6_forms(raw, entry.kind, link_local(b.first<8>()));
    } else if (b.size() == kGidBytes) {
        Ip6 gid{};
        std::ranges::copy(b, gid.begin());
        raw.add(entry.kind, ipv6_text(gid), MatchMode::token);
        add_mapped_ipv4(raw, entry.kind, gid);
    } else if (b.size() == kIpoibBytes) {
        // 4 bytes of flags and queue pair number, then the port GID.
        Ip6 gid{};
        std::ranges::copy(b.last<16>(), gid.begin());
        add_ipv6_forms(raw, entry.kind, gid);
        add_mapped_ipv4(raw, entry.kind, gid);
        raw.add(entry.kind, hex_of(b.last<8>()), MatchMode::hex);
    }
}

void expand_token(RawSet& raw, const RawEntry& entry) {
    if (const auto v4 = parse_ipv4(entry.needle)) {
        Ip6 mapped{};
        mapped[10] = 0xff;
        mapped[11] = 0xff;
        std::ranges::copy(*v4, mapped.begin() + 12);
        add_ipv6_forms(raw, entry.kind, mapped);
        // The mapped address with its IPv4 half in hex groups, as some tools print it.
        std::string mapped_hex = "::ffff:";
        mapped_hex += hex_groups(std::span<const std::uint8_t>(*v4));
        raw.add(entry.kind, std::move(mapped_hex), MatchMode::token);
        std::string host = "ip";
        for (const std::uint8_t octet : *v4) {
            host += '-';
            host += std::to_string(octet);
        }
        raw.add(entry.kind, std::move(host), MatchMode::token);
        return;
    }
    if (const auto v6 = parse_ipv6(entry.needle)) {
        raw.add(entry.kind, hex_of(*v6), MatchMode::hex);
        add_mapped_ipv4(raw, entry.kind, *v6);
        return;
    }
    if (entry.kind == IdKind::dmi || entry.kind == IdKind::serial) {
        for (std::string& id : instance_ids(entry.needle)) {
            raw.add(IdKind::instance_id, std::move(id), MatchMode::token);
        }
    }
}

using NeedleMap = std::unordered_map<std::string_view, IdKind>;

// Keys view the RawSet's needles, which must outlive the index.
struct Index {
    std::map<std::size_t, NeedleMap> hex;
    NeedleMap single;
    std::map<std::size_t, NeedleMap> multi;
};

Index build_index(const RawSet& raw) {
    Index index;
    for (const RawEntry& entry : raw.entries()) {
        const std::string_view needle = entry.needle;
        if (entry.mode == MatchMode::hex) {
            index.hex[needle.size()].emplace(needle, entry.kind);
        } else if (all_tokens_chars(needle)) {
            index.single.emplace(needle, entry.kind);
        } else {
            index.multi[needle.size()].emplace(needle, entry.kind);
        }
    }
    return index;
}

struct Hit {
    std::size_t offset; // in the original line
    IdKind kind;
};

// Reused across lines so a large output is not one allocation per line.
struct Scratch {
    std::string norm;
    std::vector<std::size_t> origin;
    std::string low;
};

void scan_hex(const Index& index, std::string_view line, Scratch& s, std::vector<Hit>& hits) {
    s.norm.clear();
    s.origin.clear();
    for (std::size_t i = 0; i < line.size(); ++i) {
        if (!is_separator(line[i])) {
            s.norm += lower(line[i]);
            s.origin.push_back(i);
        }
    }
    const std::string_view norm = s.norm;
    for (const auto& [len, needles] : index.hex) {
        if (len > norm.size()) {
            break;
        }
        for (std::size_t i = 0; i + len <= norm.size(); ++i) {
            const auto it = needles.find(norm.substr(i, len));
            if (it != needles.end()) {
                hits.push_back(Hit{.offset = s.origin[i], .kind = it->second});
            }
        }
    }
}

void scan_tokens(const Index& index, std::string_view line, Scratch& s, std::vector<Hit>& hits) {
    s.low = lowered(line);
    const std::string_view low = s.low;
    if (!index.single.empty()) {
        std::size_t i = 0;
        while (i < low.size()) {
            if (!is_token_char(low[i])) {
                ++i;
                continue;
            }
            std::size_t end = i;
            while (end < low.size() && is_token_char(low[end])) {
                ++end;
            }
            const auto it = index.single.find(low.substr(i, end - i));
            if (it != index.single.end()) {
                hits.push_back(Hit{.offset = i, .kind = it->second});
            }
            i = end;
        }
    }
    for (const auto& [len, needles] : index.multi) {
        if (len > low.size()) {
            break;
        }
        for (std::size_t i = 0; i + len <= low.size(); ++i) {
            const std::string_view window = low.substr(i, len);
            const auto it = needles.find(window);
            if (it == needles.end()) {
                continue;
            }
            const bool open_ok =
                !is_token_char(window.front()) || i == 0 || !is_token_char(low[i - 1]);
            const bool close_ok = !is_token_char(window.back()) || i + len == low.size() ||
                                  !is_token_char(low[i + len]);
            if (open_ok && close_ok) {
                hits.push_back(Hit{.offset = i, .kind = it->second});
            }
        }
    }
}

std::vector<Hit> scan(const Index& index, std::string_view line, Scratch& s) {
    std::vector<Hit> hits;
    if (!index.hex.empty()) {
        scan_hex(index, line, s, hits);
    }
    scan_tokens(index, line, s, hits);
    std::ranges::stable_sort(hits, {}, &Hit::offset);
    return hits;
}

struct Span {
    std::size_t begin, end;
    std::string locator;
};

std::string pointer_token(std::string_view key) {
    std::string out;
    for (const char c : key) {
        if (c == '~') {
            out += "~0";
        } else if (c == '/') {
            out += "~1";
        } else {
            out += c;
        }
    }
    return out;
}

// A tolerant tokenizer rather than nlohmann: the parser keeps no source positions, and a
// pointer is needed for the exact string or number a match falls in.
std::vector<Span> json_spans(std::string_view text) {
    struct Frame {
        bool object;
        std::string base, key;
        std::size_t index;
    };
    std::vector<Frame> stack;
    std::vector<Span> spans;
    const auto current = [&stack]() {
        if (stack.empty()) {
            return std::string();
        }
        const Frame& top = stack.back();
        std::string out = top.base;
        out += '/';
        out += top.object ? pointer_token(top.key) : std::to_string(top.index);
        return out;
    };
    bool expect_key = false;
    std::size_t i = 0;
    while (i < text.size()) {
        const char c = text[i];
        if (c == '{' || c == '[') {
            std::string base = current();
            stack.push_back(
                Frame{.object = c == '{', .base = std::move(base), .key = {}, .index = 0});
            expect_key = c == '{';
            ++i;
        } else if (c == '}' || c == ']') {
            if (!stack.empty()) {
                stack.pop_back();
            }
            expect_key = false;
            ++i;
        } else if (c == ',') {
            if (!stack.empty()) {
                if (stack.back().object) {
                    expect_key = true;
                } else {
                    ++stack.back().index;
                }
            }
            ++i;
        } else if (c == ':' || std::isspace(static_cast<unsigned char>(c)) != 0) {
            ++i;
        } else if (c == '"') {
            std::size_t j = i + 1;
            std::string content;
            while (j < text.size() && text[j] != '"') {
                if (text[j] == '\\' && j + 1 < text.size()) {
                    ++j;
                }
                content += text[j];
                ++j;
            }
            const std::size_t end = std::min(j + 1, text.size());
            if (expect_key && !stack.empty() && stack.back().object) {
                stack.back().key = std::move(content);
                expect_key = false;
            }
            spans.push_back(Span{.begin = i, .end = end, .locator = current()});
            i = end;
        } else {
            std::size_t j = i;
            while (j < text.size() &&
                   std::string_view(",}] \t\r\n").find(text[j]) == std::string_view::npos) {
                ++j;
            }
            spans.push_back(Span{.begin = i, .end = j, .locator = current()});
            i = j;
        }
    }
    return spans;
}

// Element path ("/topology/object/info") for tags and text, plus "/@name" for attribute values.
std::vector<Span> xml_spans(std::string_view text) {
    std::vector<std::string> stack;
    std::vector<Span> spans;
    const auto path = [&stack]() {
        std::string out;
        for (const std::string& name : stack) {
            out += '/';
            out += name;
        }
        return out;
    };
    const auto skip_past = [&text](std::size_t from, std::string_view end) {
        const std::size_t at = text.find(end, from);
        return at == std::string_view::npos ? text.size() : at + end.size();
    };
    const auto is_name_end = [](char c) {
        return c == '/' || c == '>' || c == '=' || std::isspace(static_cast<unsigned char>(c)) != 0;
    };
    std::size_t i = 0;
    while (i < text.size()) {
        if (text[i] != '<') {
            const std::size_t next = std::min(text.find('<', i), text.size());
            spans.push_back(Span{.begin = i, .end = next, .locator = path()});
            i = next;
            continue;
        }
        const std::string_view rest = text.substr(i);
        if (rest.starts_with("<!--")) {
            i = skip_past(i, "-->");
            continue;
        }
        if (rest.starts_with("<?") || rest.starts_with("<!")) {
            i = skip_past(i, ">");
            continue;
        }
        if (rest.starts_with("</")) {
            if (!stack.empty()) {
                stack.pop_back();
            }
            i = skip_past(i, ">");
            continue;
        }
        std::size_t j = i + 1;
        while (j < text.size() && !is_name_end(text[j])) {
            ++j;
        }
        stack.emplace_back(text.substr(i + 1, j - i - 1));
        spans.push_back(Span{.begin = i, .end = j, .locator = path()});
        bool closed = false;
        while (j < text.size()) {
            const char c = text[j];
            if (c == '>') {
                ++j;
                break;
            }
            if (c == '/') {
                closed = true;
                ++j;
                continue;
            }
            if (std::isspace(static_cast<unsigned char>(c)) != 0) {
                ++j;
                continue;
            }
            const std::size_t name_begin = j;
            while (j < text.size() && !is_name_end(text[j])) {
                ++j;
            }
            const std::string_view attr = text.substr(name_begin, j - name_begin);
            while (j < text.size() && text[j] != '"' && text[j] != '\'' && text[j] != '>') {
                ++j;
            }
            if (j >= text.size() || text[j] == '>') {
                continue;
            }
            const char quote = text[j];
            const std::size_t value_begin = j + 1;
            const std::size_t value_end = std::min(text.find(quote, value_begin), text.size());
            std::string locator = path();
            locator += "/@";
            locator += attr;
            spans.push_back(Span{.begin = value_begin, .end = value_end, .locator = locator});
            j = std::min(value_end + 1, text.size());
        }
        if (closed && !stack.empty()) {
            stack.pop_back();
        }
        i = j;
    }
    return spans;
}

class Locator {
  public:
    Locator(fs::path path, std::string_view text) : path_(std::move(path)), text_(text) {}

    // The span containing the offset, or failing that the last one before it.
    std::string at(std::size_t offset) {
        if (!built_) {
            build();
        }
        const auto after = std::ranges::upper_bound(spans_, offset, {}, &Span::begin);
        if (after == spans_.begin()) {
            return {};
        }
        return std::prev(after)->locator;
    }

  private:
    void build() {
        built_ = true;
        const std::string ext = path_.extension().string();
        if (ext == ".json") {
            spans_ = json_spans(text_);
        } else if (ext == ".xml") {
            spans_ = xml_spans(text_);
        }
    }

    fs::path path_;
    std::string_view text_;
    std::vector<Span> spans_;
    bool built_ = false;
};

std::string fixed_ms(std::chrono::steady_clock::duration elapsed) {
    const auto us = std::chrono::duration_cast<std::chrono::microseconds>(elapsed).count();
    constexpr long long kUsPerMs = 1000;
    std::string out = std::to_string(us / kUsPerMs);
    out += '.';
    const std::string frac = std::to_string(us % kUsPerMs);
    out.append(3 - frac.size(), '0');
    out += frac;
    return out;
}

std::string join(std::initializer_list<std::string_view> parts) {
    std::string out;
    for (const std::string_view part : parts) {
        out += part;
    }
    return out;
}

void add_net_address(RawSet& raw, const SysfsReader& sysfs, const std::string& rel) {
    constexpr std::size_t kIpoibBytes = 20;
    auto address = sysfs.read(rel);
    if (!address) {
        return;
    }
    const auto bytes = hex_bytes(normalised_hex(*address));
    const IdKind kind = bytes && bytes->size() == kIpoibBytes ? IdKind::ipoib : IdKind::mac;
    raw.add(kind, std::move(*address));
}

void collect_net(RawSet& raw, const SysfsReader& sysfs) {
    for (const std::string& name : sysfs.list("class/net")) {
        add_net_address(raw, sysfs, join({"class/net/", name, "/address"}));
    }
    // A NIC whose netdev lives in another network namespace is missing from class/net.
    for (const std::string& dev : sysfs.list(kPciDevices)) {
        const std::string base = join({kPciDevices, dev, "/net/"});
        for (const std::string& name : sysfs.list(base)) {
            add_net_address(raw, sysfs, join({base, name, "/address"}));
        }
    }
}

void collect_ib_device(RawSet& raw, const SysfsReader& sysfs, const std::string& base) {
    for (const std::string_view attr : {"node_guid", "sys_image_guid"}) {
        if (auto guid = sysfs.read(join({base, attr}))) {
            raw.add(IdKind::guid, std::move(*guid));
        }
    }
    for (const std::string& port : sysfs.list(join({base, "ports"}))) {
        const std::string gids = join({base, "ports/", port, "/gids/"});
        for (const std::string& slot : sysfs.list(gids)) {
            if (auto gid = sysfs.read(join({gids, slot}))) {
                raw.add(IdKind::gid, std::move(*gid));
            }
        }
    }
}

void collect_ib(RawSet& raw, const SysfsReader& sysfs) {
    for (const std::string& dev : sysfs.list("class/infiniband")) {
        collect_ib_device(raw, sysfs, join({"class/infiniband/", dev, "/"}));
    }
    for (const std::string& pci : sysfs.list(kPciDevices)) {
        const std::string base = join({kPciDevices, pci, "/infiniband/"});
        for (const std::string& dev : sysfs.list(base)) {
            collect_ib_device(raw, sysfs, join({base, dev, "/"}));
        }
    }
}

void collect_vpd(RawSet& raw, const SysfsReader& sysfs) {
    for (const std::string& dev : sysfs.list(kPciDevices)) {
        const std::string base = join({kPciDevices, dev, "/"});
        const std::string cls = sysfs.read(join({base, "class"})).value_or("");
        // Network controllers (class 0x02xxxx) only: RFC-0003 §3 names NIC VPD.
        if (!cls.starts_with("0x02")) {
            continue;
        }
        if (const auto vpd = sysfs.read_prefix(join({base, "vpd"}), kVpdCap)) {
            for (std::string& value : vpd_identifiers(*vpd)) {
                raw.add(IdKind::serial, std::move(value));
            }
        }
    }
}

// Firmware fills unset DMI fields with stock strings shared by many machines; they identify
// nothing and could match unrelated text.
bool is_dmi_placeholder(std::string_view low) {
    constexpr std::array<std::string_view, 12> kPlaceholders{"not specified",
                                                             "to be filled by o.e.m.",
                                                             "default string",
                                                             "none",
                                                             "system serial number",
                                                             "chassis serial number",
                                                             "base board serial number",
                                                             "not applicable",
                                                             "n/a",
                                                             "0123456789",
                                                             "123456789",
                                                             "unknown"};
    if (std::ranges::find(kPlaceholders, low) != kPlaceholders.end()) {
        return true;
    }
    // A run of one repeated character, such as "00000000" or "xxxxxxxx".
    return !low.empty() && low.find_first_not_of(low.front()) == std::string_view::npos;
}

void collect_dmi(RawSet& raw, const SysfsReader& sysfs) {
    constexpr std::string_view kDmi = "class/dmi/id/";
    for (const std::string& name : sysfs.list(kDmi)) {
        auto value = sysfs.read(join({kDmi, name}));
        if (!value) {
            continue;
        }
        const std::string low = lowered(trimmed(*value));
        if (is_dmi_placeholder(low)) {
            continue;
        }
        const std::vector<std::string> ids = instance_ids(low);
        if (ids.size() == 1 && ids.front() == low) {
            raw.add(IdKind::instance_id, std::move(*value));
        } else if (name.ends_with("_uuid")) {
            raw.add(IdKind::uuid, std::move(*value));
        } else if (name.ends_with("_serial") || name.ends_with("_asset_tag")) {
            raw.add(IdKind::dmi, std::move(*value));
        }
    }
}

// NVML reports the board ID as an integer and the loader passes it in decimal; nvidia-smi prints
// it in hex, so that spelling is searched for too.
void add_board_hex(RawSet& raw, const std::string& board_id) {
    std::uint32_t value = 0;
    const char* last = board_id.c_str() + board_id.size();
    const auto [ptr, ec] = std::from_chars(board_id.c_str(), last, value);
    if (board_id.empty() || ec != std::errc{} || ptr != last) {
        return;
    }
    std::array<char, 16> buf{};
    std::snprintf(buf.data(), buf.size(), "0x%x", value);
    raw.add(IdKind::serial, buf.data());
}

void collect_nvml(RawSet& raw, const NvmlFacts& nvml) {
    for (const NvmlGpu& gpu : nvml.gpus) {
        raw.add(IdKind::uuid, gpu.uuid);
        raw.add(IdKind::serial, gpu.serial);
        raw.add(IdKind::serial, gpu.board_id);
        add_board_hex(raw, gpu.board_id);
    }
}

void collect_interfaces(RawSet& raw) {
    ifaddrs* list = nullptr;
    if (getifaddrs(&list) != 0) {
        return;
    }
    for (const ifaddrs* it = list; it != nullptr; it = it->ifa_next) {
        if (it->ifa_addr == nullptr) {
            continue;
        }
        const int family = it->ifa_addr->sa_family;
        std::array<char, INET6_ADDRSTRLEN> buf{};
        const auto size = static_cast<socklen_t>(buf.size());
        if (family == AF_INET) {
            const auto* in = reinterpret_cast<const sockaddr_in*>(it->ifa_addr);
            if (inet_ntop(AF_INET, &in->sin_addr, buf.data(), size) != nullptr) {
                raw.add(IdKind::ipv4, buf.data());
            }
        } else if (family == AF_INET6) {
            const auto* in6 = reinterpret_cast<const sockaddr_in6*>(it->ifa_addr);
            if (inet_ntop(AF_INET6, &in6->sin6_addr, buf.data(), size) != nullptr) {
                raw.add(IdKind::ipv6, buf.data());
            }
        }
#ifdef __linux__
        else if (family == AF_PACKET) {
            // sll_addr holds 8 bytes, so a 20-byte IPoIB address arrives truncated and would be
            // a bogus value; sysfs supplies those whole.
            const auto* ll = reinterpret_cast<const sockaddr_ll*>(it->ifa_addr);
            const std::size_t len = ll->sll_halen;
            if (len <= sizeof(ll->sll_addr)) {
                raw.add(IdKind::mac, hex_of(std::span<const std::uint8_t>(ll->sll_addr, len)));
            }
        }
#endif
    }
    freeifaddrs(list);
}

void collect_hostname(RawSet& raw) {
    constexpr std::size_t kHostMax = 256;
    std::array<char, kHostMax> buf{};
    if (gethostname(buf.data(), buf.size() - 1) != 0) {
        return;
    }
    add_host_names(raw, buf.data(), "/proc/sys/kernel/domainname");
}

void collect_machine_id(RawSet& raw) {
    std::ifstream in("/etc/machine-id");
    std::string id;
    if (in && std::getline(in, id)) {
        raw.add(IdKind::machine_id, std::move(id));
    }
}

} // namespace

std::string_view to_string(IdKind kind) {
    switch (kind) {
    case IdKind::mac:
        return "mac";
    case IdKind::guid:
        return "guid";
    case IdKind::gid:
        return "gid";
    case IdKind::ipv4:
        return "ipv4";
    case IdKind::ipv6:
        return "ipv6";
    case IdKind::ipoib:
        return "ipoib";
    case IdKind::uuid:
        return "uuid";
    case IdKind::serial:
        return "serial";
    case IdKind::hostname:
        return "hostname";
    case IdKind::machine_id:
        return "machine-id";
    case IdKind::dmi:
        return "dmi";
    case IdKind::instance_id:
        return "instance-id";
    case IdKind::extra:
        return "extra";
    case IdKind::unreadable:
        return "unreadable";
    }
    return "unknown";
}

void RawSet::add(IdKind kind, std::string value) {
    const MatchMode mode = default_mode(kind);
    add(kind, std::move(value), mode);
}

void RawSet::add(IdKind kind, std::string value, MatchMode mode) {
    value = trimmed(value);
    if (is_dropped(kind, value)) {
        return;
    }
    std::string needle = mode == MatchMode::hex ? normalised_hex(value) : lowered(value);
    if (needle.empty()) {
        return;
    }
    if (mode == MatchMode::hex && (needle.find_first_not_of('0') == std::string::npos ||
                                   needle.find_first_not_of('f') == std::string::npos)) {
        return;
    }
    if (needle.size() < kMinNeedle) {
        ++skipped_short_;
        return;
    }
    if (seen_.emplace(mode, needle).second) {
        entries_.push_back(RawEntry{.kind = kind, .mode = mode, .needle = std::move(needle)});
    }
}

std::map<IdKind, std::size_t> RawSet::counts() const {
    std::map<IdKind, std::size_t> out;
    for (const RawEntry& entry : entries_) {
        ++out[entry.kind];
    }
    return out;
}

RawSet collect_raw(const SysfsReader& sysfs, const NvmlFacts* nvml,
                   const std::vector<std::string>& extra, Diagnostics& diag, bool live_host) {
    RawSet raw;
    const auto timed = [&raw, &diag](std::string_view source, const auto& collect) {
        const std::size_t before = raw.entries().size();
        const auto start = std::chrono::steady_clock::now();
        collect();
        const auto elapsed = std::chrono::steady_clock::now() - start;
        std::string line = "leak raw set: ";
        line += source;
        line += ' ';
        line += std::to_string(raw.entries().size() - before);
        line += " value(s) in ";
        line += fixed_ms(elapsed);
        line += " ms";
        diag.add(line);
    };
    timed("sysfs-net", [&] { collect_net(raw, sysfs); });
    timed("infiniband", [&] { collect_ib(raw, sysfs); });
    timed("vpd", [&] { collect_vpd(raw, sysfs); });
    timed("dmi", [&] { collect_dmi(raw, sysfs); });
    if (nvml != nullptr) {
        timed("nvml", [&] { collect_nvml(raw, *nvml); });
    }
    timed("extra", [&] {
        for (const std::string& value : extra) {
            raw.add(IdKind::extra, value);
        }
    });
    if (live_host) {
        timed("interfaces", [&] { collect_interfaces(raw); });
        timed("hostname", [&] { collect_hostname(raw); });
        timed("machine-id", [&] { collect_machine_id(raw); });
    }
    return raw;
}

void expand_derived(RawSet& raw) {
    // A copy: adding derived forms reallocates the entries.
    const std::vector<RawEntry> sources = raw.entries();
    for (const RawEntry& entry : sources) {
        if (entry.mode == MatchMode::hex) {
            expand_hex(raw, entry);
        } else {
            expand_token(raw, entry);
        }
    }
}

std::vector<Finding> search(const RawSet& raw, const std::vector<fs::path>& files) {
    const Index index = build_index(raw);
    Scratch scratch;
    // A locator or file name that would itself carry an identifier is withheld.
    const auto safe = [&index](std::string text) {
        Scratch own;
        if (!scan(index, text, own).empty()) {
            return std::string(kWithheld);
        }
        return text;
    };
    std::vector<Finding> out;
    for (const fs::path& path : files) {
        const std::string file = safe(path.filename().string());
        std::ifstream in(path, std::ios::binary);
        if (!in) {
            out.push_back(
                Finding{.file = file, .line = 0, .locator = {}, .kind = IdKind::unreadable});
            continue;
        }
        const std::string text{std::istreambuf_iterator<char>(in),
                               std::istreambuf_iterator<char>()};
        Locator locator(path, text);
        std::set<std::tuple<int, std::string, IdKind>> seen;
        std::size_t start = 0;
        int line_no = 0;
        while (start < text.size()) {
            ++line_no;
            const std::size_t newline = std::min(text.find('\n', start), text.size());
            std::string_view line = std::string_view(text).substr(start, newline - start);
            if (line.ends_with('\r')) {
                line.remove_suffix(1);
            }
            for (const Hit& hit : scan(index, line, scratch)) {
                std::string where = safe(locator.at(start + hit.offset));
                if (seen.emplace(line_no, where, hit.kind).second) {
                    out.push_back(Finding{.file = file,
                                          .line = line_no,
                                          .locator = std::move(where),
                                          .kind = hit.kind});
                }
            }
            start = newline + 1;
        }
    }
    return out;
}

void add_host_names(RawSet& raw, const std::string& hostname, const fs::path& domainname_file) {
    raw.add(IdKind::hostname, hostname);
    raw.add(IdKind::hostname, hostname.substr(0, hostname.find('.')));
    std::ifstream in(domainname_file);
    std::string line;
    if (!in || !std::getline(in, line)) {
        return;
    }
    const std::string domain = trimmed(line);
    if (domain.empty() || domain == "(none)") {
        return;
    }
    std::string fqdn = hostname;
    fqdn += '.';
    fqdn += domain;
    raw.add(IdKind::hostname, std::move(fqdn));
}

std::vector<std::string> read_extra_identifiers(const fs::path& path) {
    std::vector<std::string> out;
    std::ifstream in(path);
    std::string line;
    while (std::getline(in, line)) {
        std::string value = trimmed(line);
        if (!value.empty() && !value.starts_with('#')) {
            out.push_back(std::move(value));
        }
    }
    return out;
}

std::string to_string(const Finding& finding) {
    std::string out = finding.file;
    if (finding.kind == IdKind::unreadable) {
        out += ":0 unreadable";
        return out;
    }
    out += ':';
    out += std::to_string(finding.line);
    if (!finding.locator.empty()) {
        out += ' ';
        out += finding.locator;
    }
    out += ": ";
    out += to_string(finding.kind);
    return out;
}

std::vector<std::string> vpd_identifiers(std::string_view vpd) {
    constexpr std::uint8_t kLargeResource = 0x80;
    constexpr std::uint8_t kVpdReadOnly = 0x10;
    constexpr std::uint8_t kVpdWritable = 0x11;
    constexpr unsigned kSmallEnd = 0x0f;
    std::vector<std::string> out;
    const auto byte = [&vpd](std::size_t at) { return static_cast<std::uint8_t>(vpd[at]); };
    std::size_t pos = 0;
    while (pos < vpd.size()) {
        const std::uint8_t tag = byte(pos);
        if ((tag & kLargeResource) == 0) {
            // Small resource: bits 6-3 name the item, bits 2-0 give its length.
            if (((tag >> 3U) & 0x0fU) == kSmallEnd) {
                break;
            }
            pos += 1 + (tag & 0x07U);
            continue;
        }
        if (pos + 3 > vpd.size()) {
            break;
        }
        const std::size_t len = static_cast<std::size_t>(byte(pos + 1)) |
                                (static_cast<std::size_t>(byte(pos + 2)) << 8U);
        const std::size_t data = pos + 3;
        const std::string_view body = vpd.substr(data, std::min(len, vpd.size() - data));
        const auto name = static_cast<std::uint8_t>(tag & 0x7fU);
        if (name == kVpdReadOnly || name == kVpdWritable) {
            std::size_t k = 0;
            while (k + 3 <= body.size()) {
                const std::string_view keyword = body.substr(k, 2);
                const std::size_t field = static_cast<std::uint8_t>(body[k + 2]);
                const std::string_view value =
                    body.substr(k + 3, std::min(field, body.size() - k - 3));
                const bool vendor = keyword[0] == 'V' &&
                                    (std::isdigit(static_cast<unsigned char>(keyword[1])) != 0 ||
                                     std::isupper(static_cast<unsigned char>(keyword[1])) != 0);
                if (keyword == "SN" || vendor) {
                    std::string text = trimmed(value);
                    if (!text.empty()) {
                        out.push_back(std::move(text));
                    }
                }
                k += 3 + field;
            }
        }
        pos = data + len;
    }
    return out;
}

} // namespace ostia::fabric::topology::capture
