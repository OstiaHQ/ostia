#include "core/pipeline.hpp"

#include <algorithm>
#include <array>
#include <cerrno>
#include <chrono>
#include <cstdint>
#include <cstdlib>
#include <ctime>
#include <fstream>
#include <iostream>
#include <iterator>
#include <set>
#include <stdexcept>
#include <system_error>
#include <utility>

#include "core/cleanup.hpp"
#include "core/emit_xml.hpp"
#include "core/leak.hpp"
#include "core/manifest.hpp"
#include "core/nics.hpp"
#include "topology/builder.hpp"
#include "topology/error.hpp"
#include "topology/fixture_source.hpp"
#include "topology/hwloc_facts.hpp"
#include "topology/identity.hpp"
#include "topology/schema.hpp"

namespace ostia::fabric::topology::capture {

namespace {

namespace fs = std::filesystem;
using nlohmann::json;
using Clock = std::chrono::steady_clock;

// The data files a manifest may list (RFC-0003 §4); nothing else is ever removed from --out.
constexpr std::array<std::string_view, 5> kDataFiles{"hwloc.xml", "nvml.json", "nics.json",
                                                     "links.json", "meta.json"};
constexpr unsigned kVendorNvidia = 0x10de;
constexpr unsigned kClassDisplay = 0x03;

std::string ms_since(Clock::time_point start) {
    const auto us =
        std::chrono::duration_cast<std::chrono::microseconds>(Clock::now() - start).count();
    constexpr long long kUsPerMs = 1000;
    std::string out = std::to_string(us / kUsPerMs);
    out += '.';
    const std::string frac = std::to_string(us % kUsPerMs);
    out.append(3 - frac.size(), '0');
    out += frac;
    out += " ms";
    return out;
}

std::string utc_now() {
    const std::time_t now = std::chrono::system_clock::to_time_t(std::chrono::system_clock::now());
    std::tm tm{};
    gmtime_r(&now, &tm);
    std::array<char, 32> buf{};
    std::strftime(buf.data(), buf.size(), "%Y-%m-%dT%H:%M:%SZ", &tm);
    return buf.data();
}

std::optional<std::string> read_file(const fs::path& path) {
    std::ifstream in(path, std::ios::binary);
    if (!in) {
        return std::nullopt;
    }
    std::string text{std::istreambuf_iterator<char>(in), std::istreambuf_iterator<char>()};
    if (in.bad()) {
        return std::nullopt;
    }
    return text;
}

std::string count_of(std::size_t n, std::string_view noun) {
    std::string out = std::to_string(n);
    out += ' ';
    out += noun;
    return out;
}

// Everything the writers consume, read once so capture and live_facts see the same sources.
struct Sources {
    PcieMaxMap pcie_max;
    std::set<std::string> hwloc_devices; // bus IDs of hwloc's PCI devices, bridges excluded
    bool nvml_required = false;
    std::optional<NvmlFacts> nvml;
    std::optional<MissingFile> nvml_missing;
    std::optional<std::vector<VerbsPort>> ports;
    std::vector<NicFacts> nics;
    std::size_t hidden_rdma = 0; // InfiniBand devices sysfs shows while ibverbs listed none
};

Sources read_sources(const Inputs& in, Diagnostics& diag) {
    Sources s;
    for (hwloc_obj_t p = hwloc_get_next_pcidev(in.topo, nullptr); p != nullptr;
         p = hwloc_get_next_pcidev(in.topo, p)) {
        if (std::optional<std::string> id = bus_id_of(p)) {
            s.hwloc_devices.insert(std::move(*id));
        }
        // RFC-0003 §1: nvml.json is required exactly when hwloc lists an NVIDIA display device.
        s.nvml_required = s.nvml_required || (p->attr->pcidev.vendor_id == kVendorNvidia &&
                                              (p->attr->pcidev.class_id >> 8) == kClassDisplay);
    }
    diag.add("hwloc: " + count_of(s.hwloc_devices.size(), "PCI device(s)"));

    auto start = Clock::now();
    if (!s.nvml_required) {
        diag.add("nvml: not queried (hwloc lists no NVIDIA display device)");
    } else if (in.nvml == nullptr) {
        s.nvml_missing = MissingFile{.file = "nvml.json", .reason = "nvml_unavailable"};
        diag.add("nvml: library unavailable");
    } else {
        Result<NvmlFacts> facts = in.nvml->query();
        if (facts.ok()) {
            s.nvml = facts.value();
            diag.add("nvml: " + count_of(s.nvml->gpus.size(), "GPU(s) in ") + ms_since(start));
        } else {
            s.nvml_missing = MissingFile{.file = "nvml.json", .reason = facts.error()};
            diag.add("nvml: query failed (" + facts.error() + ") in " + ms_since(start));
        }
    }

    start = Clock::now();
    if (in.verbs != nullptr) {
        s.ports = in.verbs->ports();
    }
    if (s.ports) {
        // A port hwloc does not list (rxe, siw, a device outside a fake root) would be a dangling
        // reference in the model, so it is dropped here rather than failing the replay.
        const auto dropped = std::ranges::remove_if(*s.ports, [&s](const VerbsPort& port) {
            return !s.hwloc_devices.contains(port.bus_id);
        });
        const auto skipped = static_cast<std::size_t>(std::ranges::distance(dropped));
        s.ports->erase(dropped.begin(), dropped.end());
        if (skipped > 0) {
            diag.add("verbs: skipped " +
                     count_of(skipped, "port(s) whose bus ID hwloc does not list"));
        }
        diag.add("verbs: " + count_of(s.ports->size(), "port(s) in ") + ms_since(start));
        if (s.ports->empty()) {
            s.hidden_rdma = in.sysfs->list("class/infiniband").size();
        }
        if (s.hidden_rdma > 0) {
            diag.add("verbs: sysfs shows " + count_of(s.hidden_rdma, "InfiniBand device(s)") +
                     " but ibverbs listed none");
        }
    } else {
        diag.add("verbs: probe unavailable");
    }

    start = Clock::now();
    s.nics = scan_nics(*in.sysfs, s.ports.value_or(std::vector<VerbsPort>{}), diag);
    const auto dropped = std::ranges::remove_if(
        s.nics, [&s](const NicFacts& nic) { return !s.hwloc_devices.contains(nic.bus_id); });
    const auto skipped = static_cast<std::size_t>(std::ranges::distance(dropped));
    s.nics.erase(dropped.begin(), dropped.end());
    if (skipped > 0) {
        diag.add("nics: skipped " + count_of(skipped, "NIC(s) whose bus ID hwloc does not list"));
    }
    diag.add("nics: " + count_of(s.nics.size(), "NIC(s) in ") + ms_since(start));

    s.pcie_max = read_pcie_max(*in.sysfs);
    return s;
}

// A failure that ends the capture with the given manifest error code.
struct Abort {
    std::string code;
};

json read_links(const fs::path& path, Diagnostics& diag) {
    const std::optional<std::string> text = read_file(path);
    json doc;
    try {
        doc = text ? json::parse(*text) : json();
    } catch (const json::parse_error&) {
        doc = json();
    }
    if (!text || doc.is_null()) {
        diag.add("links: the --links file cannot be read or is not JSON");
        throw Abort{"links_read"};
    }
    const std::vector<SchemaError> errors = validate("links", doc);
    for (const SchemaError& e : errors) {
        diag.add("links: schema violation at " + (e.path.empty() ? "/" : e.path));
    }
    if (!errors.empty()) {
        throw Abort{"links_schema"};
    }
    return doc;
}

struct Staged {
    std::string name, bytes;
};

const char* schema_for(std::string_view name) {
    if (name == "nvml.json") {
        return "nvml";
    }
    if (name == "nics.json") {
        return "nics";
    }
    if (name == "links.json") {
        return "links";
    }
    return name == "meta.json" ? "meta" : nullptr;
}

// Validates the staged files as read back from disk, so what passes is what was written.
bool validate_staged(const fs::path& dir, const std::vector<Staged>& files, Diagnostics& diag) {
    const auto start = Clock::now();
    bool ok = true;
    for (const Staged& file : files) {
        const char* schema = schema_for(file.name);
        if (schema == nullptr) {
            continue;
        }
        const std::optional<std::string> text = read_file(dir / file.name);
        json doc;
        try {
            doc = text ? json::parse(*text) : json();
        } catch (const json::parse_error&) {
            doc = json();
        }
        std::string line = "schema: ";
        line += file.name;
        if (doc.is_null()) {
            line += " is not readable JSON";
            diag.add(line);
            ok = false;
            continue;
        }
        line += " violation at ";
        // The pointer only: a message can quote the offending value.
        for (const SchemaError& e : validate(schema, doc)) {
            diag.add(line + (e.path.empty() ? "/" : e.path));
            ok = false;
        }
    }
    diag.add("validate: " + count_of(files.size(), "file(s) in ") + ms_since(start));
    return ok;
}

// The scratch directory (RFC-0003 §1: private, outside --out, removed on every exit).
class Scratch {
  public:
    Scratch() {
        std::string pattern = (fs::temp_directory_path() / "ostia-topo-capture-XXXXXX").string();
        if (::mkdtemp(pattern.data()) == nullptr) {
            throw std::system_error(errno, std::generic_category(), "mkdtemp");
        }
        path_ = pattern;
        track_for_cleanup(path_, true);
    }
    Scratch(const Scratch&) = delete;
    Scratch& operator=(const Scratch&) = delete;
    Scratch(Scratch&&) = delete;
    Scratch& operator=(Scratch&&) = delete;
    ~Scratch() {
        std::error_code ec;
        fs::remove_all(path_, ec);
    }
    [[nodiscard]] const fs::path& path() const { return path_; }

  private:
    fs::path path_;
};

void print_error(std::string_view error, std::string_view fix) {
    std::cerr << "error: " << error << "\n"
              << "  rule: a capture is written only into an empty directory or over a previous "
                 "capture (RFC-0003 §4)\n"
              << "  fix: " << fix << "\n"
              << "  see: RFC-0003 §4\n";
}

// Empty, created, or a previous capture whose listed files, manifest and diagnostics are removed.
bool prepare_out(const fs::path& out, Diagnostics& diag) {
    std::error_code ec;
    if (!fs::exists(out, ec)) {
        fs::create_directories(out);
        return true;
    }
    if (!fs::is_directory(out, ec)) {
        print_error("--out exists and is not a directory", "choose a directory path for --out");
        diag.add("out: refused, not a directory");
        return false;
    }
    if (fs::is_empty(out, ec)) {
        return true;
    }
    json previous;
    if (const std::optional<std::string> text = read_file(out / "manifest.json")) {
        try {
            previous = json::parse(*text);
        } catch (const json::parse_error&) {
            previous = json();
        }
    }
    if (!previous.is_object()) {
        print_error("--out is a non-empty directory without a previous capture's manifest.json",
                    "remove <out> or choose an empty directory");
        diag.add("out: refused, non-empty without a readable manifest.json");
        return false;
    }
    std::size_t removed = 0;
    if (const auto files = previous.find("files"); files != previous.end() && files->is_object()) {
        for (const std::string_view name : kDataFiles) {
            if (files->contains(name) && fs::remove(out / name, ec)) {
                ++removed;
            }
        }
    }
    for (const char* name : {"manifest.json", "manifest.json.tmp", "diagnostics.txt"}) {
        fs::remove(out / name, ec);
    }
    diag.add("out: removed a previous capture (" + count_of(removed, "data file(s))"));
    return true;
}

// Removes a directory tree when it goes out of scope, whatever the exit path.
class RemoveTree {
  public:
    explicit RemoveTree(fs::path dir) : dir_(std::move(dir)) {}
    RemoveTree(const RemoveTree&) = delete;
    RemoveTree& operator=(const RemoveTree&) = delete;
    RemoveTree(RemoveTree&&) = delete;
    RemoveTree& operator=(RemoveTree&&) = delete;
    ~RemoveTree() {
        std::error_code ec;
        fs::remove_all(dir_, ec);
    }

  private:
    fs::path dir_;
};

class Run {
  public:
    Run(const Inputs& in, const fs::path& out, Diagnostics& diag)
        : in_(in), out_(out), diag_(diag) {}

    CaptureOutcome go() {
        try {
            const Scratch scratch;
            stage_and_check(scratch.path());
        } catch (const Abort& a) {
            fail(a.code, false);
        } catch (const std::system_error&) {
            diag_.add("capture: an output file could not be written");
            fail("write_failed", false);
        } catch (const std::exception&) {
            diag_.add("capture: unexpected exception");
            fail("internal", false);
        } catch (...) {
            diag_.add("capture: unexpected non-standard exception");
            fail("internal", false);
        }
        return finish();
    }

  private:
    void fail(std::string code, bool leak_or_schema) {
        (leak_or_schema ? leak_or_schema_ : other_failure_) = true;
        manifest_.errors.push_back(std::move(code));
    }

    void stage_and_check(const fs::path& scratch) {
        if (in_.meta.node_index) {
            diag_.add("node index: " + std::to_string(*in_.meta.node_index));
        }
        const Sources s = read_sources(in_, diag_);
        if (s.nvml_missing) {
            manifest_.missing.push_back(*s.nvml_missing);
            partial_ = true;
        }
        rdma_line_ = s.ports ? "rdma probe ok, " + count_of(s.ports->size(), "port(s)")
                             : std::string("rdma probe unavailable");
        nic_count_ = s.nics.size();
        gpu_line_ = s.nvml ? count_of(s.nvml->gpus.size(), "GPU(s)")
                           : (s.nvml_missing ? "missing (" + s.nvml_missing->reason + ")"
                                             : std::string("not required"));
        if (s.hidden_rdma > 0) {
            warnings_.emplace_back("sysfs shows InfiniBand devices but ibverbs listed none; "
                                   "nics.json records no RDMA ports");
        }

        const NvmlFacts* nvml = s.nvml ? &*s.nvml : nullptr;
        RawSet raw = collect_raw(*in_.sysfs, nvml, in_.extra, diag_, in_.live_host);
        expand_derived(raw);
        raw_count_ = raw.entries().size();
        diag_.add("leak raw set: " + count_of(raw_count_, "form(s) after derivation, ") +
                  count_of(raw.skipped_short(), "skipped as too short"));

        auto start = Clock::now();
        std::string xml = emit_xml(in_.topo, s.pcie_max, diag_);
        if (in_.hooks.before_reimport) {
            in_.hooks.before_reimport(xml);
        }
        try {
            reimport_check(xml);
        } catch (const TopologyError&) {
            diag_.add("emit: hwloc.xml does not re-import with the replay flags");
            throw Abort{"xml_reimport"};
        }
        diag_.add("emit: hwloc.xml in " + ms_since(start));

        std::vector<Staged> files;
        files.push_back({.name = "hwloc.xml", .bytes = std::move(xml)});
        if (nvml != nullptr) {
            files.push_back({.name = "nvml.json", .bytes = dump(nvml_json(*nvml))});
        }
        files.push_back({.name = "nics.json", .bytes = dump(nics_json(s.nics, s.ports))});
        if (in_.links) {
            files.push_back({.name = "links.json", .bytes = dump(read_links(*in_.links, diag_))});
        }
        files.push_back({.name = "meta.json", .bytes = dump(meta_json(in_.meta, nvml, utc_now()))});

        for (const Staged& file : files) {
            track_for_cleanup(scratch / file.name, false);
            write_file_synced(scratch / file.name, file.bytes);
        }
        if (in_.hooks.before_validate) {
            in_.hooks.before_validate(scratch);
        }
        if (!validate_staged(scratch, files, diag_)) {
            fail("schema", true);
            return;
        }
        manifest_.sanitized = true;

        // RFC-0003 §3: diagnostics.txt is searched as written so far; findings are appended after.
        start = Clock::now();
        const fs::path staged_diag = scratch / "diagnostics.txt";
        track_for_cleanup(staged_diag, false);
        write_file_synced(staged_diag, diag_.render() + "\n");
        std::vector<fs::path> paths;
        paths.reserve(files.size() + 1);
        for (const Staged& file : files) {
            paths.push_back(scratch / file.name);
        }
        paths.push_back(staged_diag);
        const std::vector<Finding> findings = search(raw, paths);
        diag_.add("leak search: " + count_of(paths.size(), "file(s), ") +
                  count_of(findings.size(), "finding(s) in ") + ms_since(start));
        bool leak = false;
        bool unreadable = false;
        for (const Finding& f : findings) {
            diag_.add("leak: " + to_string(f));
            (f.kind == IdKind::unreadable ? unreadable : leak) = true;
        }
        manifest_.leak_check = findings.empty() ? LeakCheck::passed : LeakCheck::failed;
        leak_findings_ = findings.size();
        if (leak) {
            fail("leak", true);
        }
        // A file the search could not read cannot be shown clean, but it is not a leak.
        if (unreadable) {
            fail("output_unreadable", false);
        }
        if (leak || unreadable) {
            return;
        }

        start = Clock::now();
        std::string id;
        try {
            id = topo1(build(FixtureSource(scratch).facts()));
        } catch (const TopologyError& e) {
            diag_.add("replay: " + e.code + " in " + (e.file.empty() ? "(model)" : e.file));
            throw Abort{"replay"};
        }
        diag_.add("replay: topo1 in " + ms_since(start));

        for (const Staged& file : files) {
            const fs::path path = out_ / file.name;
            track_for_cleanup(path, false);
            written_.push_back(path);
            write_file_synced(path, file.bytes);
            manifest_.files[file.name] = file_hash(file.bytes);
        }
        manifest_.topology_id = std::move(id);
    }

    CaptureOutcome finish() {
        int code = exit_for(leak_or_schema_, other_failure_, partial_);
        if (code == 1 || code == 3) {
            std::error_code ec;
            for (const fs::path& path : written_) {
                fs::remove(path, ec);
            }
            manifest_.status = Status::failed;
            manifest_.files.clear();
            manifest_.topology_id.reset();
        } else {
            manifest_.status = partial_ ? Status::partial : Status::complete;
        }
        diag_.add("capture: exit " + std::to_string(code));

        const fs::path diag_path = out_ / "diagnostics.txt";
        track_for_cleanup(out_ / "manifest.json.tmp", false);
        try {
            write_file_synced(diag_path, diag_.render() + "\n");
            write_manifest_atomic(out_, manifest_json(manifest_));
        } catch (const std::system_error&) {
            std::cerr << "error: the manifest or diagnostics.txt could not be written\n";
            code = 1;
            manifest_.topology_id.reset();
        }
        summary(code);
        return {.exit_code = code, .topology_id = manifest_.topology_id};
    }

    void summary(int code) const {
        std::cerr << "  gpus: " << gpu_line_ << "\n"
                  << "  nics: " << nic_count_ << " NIC(s); " << rdma_line_ << "\n";
        if (manifest_.leak_check == LeakCheck::not_run) {
            std::cerr << "  leak check: not run\n";
        } else {
            std::cerr << "  leak check: "
                      << (manifest_.leak_check == LeakCheck::passed ? "passed" : "failed") << " ("
                      << raw_count_ << " raw form(s), " << leak_findings_ << " finding(s))\n";
        }
        for (const std::string& code_name : manifest_.errors) {
            std::cerr << "  error " << code_name << ": " << error_message(code_name) << "\n";
        }
        for (const std::string& w : warnings_) {
            std::cerr << "warning: " << w << "\n";
        }
        std::cerr << "  status: " << to_string(manifest_.status) << ", exit " << code << "\n";
    }

    const Inputs& in_;
    const fs::path& out_;
    Diagnostics& diag_;
    Manifest manifest_;
    std::vector<fs::path> written_;
    std::vector<std::string> warnings_;
    std::string gpu_line_ = "not read", rdma_line_ = "not read";
    std::size_t nic_count_ = 0, raw_count_ = 0, leak_findings_ = 0;
    bool leak_or_schema_ = false, other_failure_ = false, partial_ = false;
};

} // namespace

CaptureOutcome capture(const Inputs& in, const fs::path& out, Diagnostics& diag) {
    std::cerr << "ostia-topo-capture: recording provider \"" << in.meta.provider
              << "\", instance type \"" << in.meta.instance_type << "\"\n";
    try {
        if (!prepare_out(out, diag)) {
            return {.exit_code = 1, .topology_id = std::nullopt};
        }
    } catch (const std::system_error&) {
        print_error("--out cannot be created or cleaned", "check the --out path and permissions");
        return {.exit_code = 1, .topology_id = std::nullopt};
    }
    const CleanupScope scope;
    return Run(in, out, diag).go();
}

CaptureOutcome print_id(const Inputs& in, Diagnostics& diag) {
    const CleanupScope scope;
    std::string pattern = (fs::temp_directory_path() / "ostia-topo-id-XXXXXX").string();
    if (::mkdtemp(pattern.data()) == nullptr) {
        std::cerr << "error: cannot create a temporary directory for --print-id\n";
        return {.exit_code = 1, .topology_id = std::nullopt};
    }
    const fs::path dir = pattern;
    track_for_cleanup(dir, true);
    track_for_cleanup(dir / "diagnostics.txt", false);
    track_for_cleanup(dir / "manifest.json", false);
    const RemoveTree remove(dir);
    return capture(in, dir, diag);
}

Facts live_facts(const Inputs& in) {
    Diagnostics diag;
    const Sources s = read_sources(in, diag);
    Facts facts;
    extract_hwloc_facts(in.topo, &s.pcie_max, facts);
    facts.nvml = s.nvml ? nvml_json(*s.nvml) : json();
    facts.nics = nics_json(s.nics, s.ports);
    try {
        facts.links = in.links ? read_links(*in.links, diag) : json();
    } catch (const Abort& a) {
        throw TopologyError(a.code, "links.json", "the --links file is not a valid links.json");
    }
    return facts;
}

CaptureOutcome write_failed_capture(const fs::path& out, std::string_view code, Diagnostics& diag) {
    Manifest manifest;
    manifest.errors.emplace_back(code);
    diag.add("capture: failed before the pipeline ran (" + std::string(code) + ")");
    try {
        write_file_synced(out / "diagnostics.txt", diag.render() + "\n");
        write_manifest_atomic(out, manifest_json(manifest));
    } catch (const std::system_error&) {
        std::cerr << "error: the manifest or diagnostics.txt could not be written\n";
    }
    return {.exit_code = 1, .topology_id = std::nullopt};
}

} // namespace ostia::fabric::topology::capture
