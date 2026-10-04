#include <charconv>
#include <exception>
#include <filesystem>
#include <iostream>
#include <memory>
#include <optional>
#include <span>
#include <string>
#include <string_view>
#include <system_error>
#include <unistd.h>
#include <utility>
#include <vector>

#include "core/cleanup.hpp"
#include "core/diagnostics.hpp"
#include "core/leak.hpp"
#include "core/pipeline.hpp"
#include "core/sysfs.hpp"
#include "live/live.hpp"

namespace fs = std::filesystem;
using namespace ostia::fabric::topology::capture;

namespace {

// Exit 2 means a partial capture (RFC-0003 §4), so a usage error is 1.
constexpr int kUsage = 1;

constexpr std::string_view kUsageText =
    "usage: ostia-topo-capture --out <dir> --provider <name> --instance-type <type>\n"
    "                          [--links <links.json>] [--node-index <n>]\n"
    "                          [--extra-identifiers <file>]\n"
    "       ostia-topo-capture --print-id [--provider <name>] [--instance-type <type>] [...]\n"
    "       ostia-topo-capture --help | --version\n"
    "\n"
    "Captures this machine's topology into <dir> (RFC-0003): hwloc.xml, nvml.json, nics.json,\n"
    "meta.json, links.json when --links is given, then diagnostics.txt and manifest.json.\n"
    "Exit 0 complete, 2 partial, 1 failed, 3 leak or schema violation.\n"
    "\n"
    "  --out <dir>                 an empty directory or a previous capture's directory\n"
    "  --provider <name>           recorded in meta.json; never read from a metadata service\n"
    "  --instance-type <type>      recorded in meta.json\n"
    "  --links <links.json>        measured links to include (RFC-0003 §8)\n"
    "  --node-index <n>            a non-negative integer, recorded in diagnostics.txt only\n"
    "  --extra-identifiers <file>  more identifiers for the leak check, one per line\n"
    "  --print-id                  run the whole capture in a private temporary directory and\n"
    "                              print only its topology id\n";

struct Args {
    std::optional<fs::path> out, links, extra;
    // "unknown" stands when --print-id runs without them; --out requires both.
    std::string provider = "unknown", instance_type = "unknown";
    bool has_provider = false, has_instance_type = false;
    std::optional<int> node_index;
    fs::path sysfs_root = "/sys";
    bool print_id = false, help = false, version = false, no_verbs = false;
};

// Option values are never echoed: a mistyped value can be an identifier of this machine.
struct UsageError {
    std::string error, rule, fix;
};

[[noreturn]] void usage_error(std::string error, std::string rule, std::string fix) {
    throw UsageError{.error = std::move(error), .rule = std::move(rule), .fix = std::move(fix)};
}

std::optional<int> non_negative(const std::string& text) {
    int value = 0;
    const char* last = text.c_str() + text.size();
    const auto [ptr, ec] = std::from_chars(text.c_str(), last, value);
    if (text.empty() || ec != std::errc{} || ptr != last || value < 0) {
        return std::nullopt;
    }
    return value;
}

Args parse(std::span<char* const> argv) {
    Args args;
    for (std::size_t i = 1; i < argv.size(); ++i) {
        std::string_view arg = argv[i];
        std::optional<std::string_view> inline_value;
        if (const std::size_t eq = arg.find('='); arg.starts_with("--") && eq != arg.npos) {
            inline_value = arg.substr(eq + 1);
            arg = arg.substr(0, eq);
        }
        const auto value = [&]() -> std::string {
            if (inline_value) {
                return std::string(*inline_value);
            }
            if (i + 1 >= argv.size()) {
                usage_error(std::string(arg) + " needs a value",
                            "every option except --print-id, --help and --version takes a value",
                            "see ostia-topo-capture --help");
            }
            return argv[++i];
        };
        const auto flag = [&](bool& target) {
            if (inline_value) {
                usage_error(std::string(arg) + " takes no value", "flags take no value",
                            "drop the value after " + std::string(arg));
            }
            target = true;
        };
        if (arg == "--out") {
            args.out = value();
        } else if (arg == "--provider") {
            args.provider = value();
            args.has_provider = true;
        } else if (arg == "--instance-type") {
            args.instance_type = value();
            args.has_instance_type = true;
        } else if (arg == "--links") {
            args.links = value();
        } else if (arg == "--extra-identifiers") {
            args.extra = value();
        } else if (arg == "--node-index") {
            args.node_index = non_negative(value());
            if (!args.node_index) {
                usage_error("--node-index is not a non-negative integer",
                            "--node-index takes a decimal integer from 0",
                            "pass the node's index in its pair, such as --node-index 0");
            }
        } else if (arg == "--sysfs-root") {
            args.sysfs_root = value();
        } else if (arg == "--print-id") {
            flag(args.print_id);
        } else if (arg == "--no-verbs") {
            flag(args.no_verbs);
        } else if (arg == "--help" || arg == "-h") {
            flag(args.help);
        } else if (arg == "--version") {
            flag(args.version);
        } else if (arg.starts_with("-")) {
            usage_error("unknown option " + std::string(arg),
                        "only the documented options are accepted",
                        "see ostia-topo-capture --help");
        } else {
            usage_error("unexpected positional argument", "ostia-topo-capture takes options only",
                        "see ostia-topo-capture --help");
        }
    }
    return args;
}

// --print-id replaces --out, and a kept capture records the provider and instance type its caller
// names, since the tool never asks a metadata service (RFC-0003 §1).
void check(const Args& args) {
    if (args.help || args.version) {
        return;
    }
    if (args.out && args.print_id) {
        usage_error("--out and --print-id are exclusive",
                    "--print-id captures into a private temporary directory, never into --out",
                    "pass one of them");
    }
    if (!args.out && !args.print_id) {
        usage_error("one of --out or --print-id is required",
                    "the capture is written to --out, or reduced to its id by --print-id",
                    "pass --out <dir> or --print-id");
    }
    if (args.out && (!args.has_provider || !args.has_instance_type)) {
        usage_error("--provider and --instance-type are required with --out",
                    "meta.json records them, and the tool never asks a metadata service",
                    "pass --provider <name> --instance-type <type>");
    }
    if (args.extra) {
        std::error_code ec;
        if (!fs::is_regular_file(*args.extra, ec)) {
            usage_error("the --extra-identifiers file cannot be read",
                        "a missing file would silently weaken the leak check",
                        "check the path, or drop --extra-identifiers");
        }
    }
}

int run(const Args& args, Diagnostics& diag) {
    if (::geteuid() == 0) {
        diag.add("user: running as root, which is neither needed nor recommended (RFC-0003 §1)");
    }
    const TopologyPtr topo = load_live_topology(diag);
    if (topo == nullptr) {
        std::cerr << "error: hwloc could not load the topology of this machine\n"
                  << "  rule: hwloc.xml is part of every capture (RFC-0003 §1)\n"
                  << "  fix: check that hwloc can read /sys; `lstopo` shows its own error\n"
                  << "  see: RFC-0003 §1\n";
        return args.out ? write_failed_capture(*args.out, "hwloc_load", diag).exit_code : 1;
    }
    const SysfsReader sysfs(args.sysfs_root);
    const std::unique_ptr<NvmlApi> nvml = load_nvml(diag);
    std::unique_ptr<VerbsApi> verbs;
    if (args.no_verbs) {
        diag.add("verbs: probe disabled by --no-verbs");
    } else {
        verbs = make_verbs(sysfs, diag);
    }

    Inputs in;
    in.topo = topo.get();
    in.nvml = nvml.get();
    in.verbs = verbs.get();
    in.sysfs = &sysfs;
    in.meta = MetaFlags{.provider = args.provider,
                        .instance_type = args.instance_type,
                        .node_index = args.node_index};
    in.links = args.links;
    if (args.extra) {
        in.extra = read_extra_identifiers(*args.extra);
    }
    in.live_host = true;

    if (args.out) {
        return capture(in, *args.out, diag).exit_code;
    }
    const CaptureOutcome outcome = print_id(in, diag);
    if (outcome.topology_id) {
        std::cout << *outcome.topology_id << "\n";
    }
    return outcome.exit_code;
}

} // namespace

int main(int argc, char** argv) {
    Args args;
    try {
        args = parse(std::span<char* const>(argv, static_cast<std::size_t>(argc)));
        check(args);
    } catch (const UsageError& e) {
        std::cerr << "error: " << e.error << "\n"
                  << "  rule: " << e.rule << "\n"
                  << "  fix: " << e.fix << "\n"
                  << "  see: RFC-0003 §1\n";
        return kUsage;
    }
    if (args.help) {
        std::cout << kUsageText;
        return 0;
    }
    if (args.version) {
        std::cout << "ostia-topo-capture " << OSTIA_TOOL_VERSION << "\n";
        return 0;
    }

    // The outermost scope, so SIGINT and SIGTERM clean up from here to exit, discovery included.
    const CleanupScope scope;
    Diagnostics diag;
    try {
        return run(args, diag);
    } catch (const std::exception&) {
        diag.add("capture: unexpected exception outside the pipeline");
    } catch (...) {
        diag.add("capture: unexpected non-standard exception outside the pipeline");
    }
    std::cerr << "error: unexpected internal error (internal)\n";
    return args.out ? write_failed_capture(*args.out, "internal", diag).exit_code : 1;
}
