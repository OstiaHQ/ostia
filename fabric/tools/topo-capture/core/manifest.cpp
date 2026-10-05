#include "core/manifest.hpp"

#include <array>
#include <cerrno>
#include <fcntl.h>
#include <stdexcept>
#include <sys/stat.h>
#include <system_error>
#include <unistd.h>
#include <utility>

#include "core/writers.hpp"
#include "topology/schema.hpp"
#include "topology/sha256.hpp"

namespace ostia::fabric::topology::capture {

namespace {

using nlohmann::json;
namespace fs = std::filesystem;

struct ErrorEntry {
    std::string_view code, message;
};

// One message per code, written for the person reading the manifest; none names a value from the
// machine. links_schema names the supported versions, which must track kSchemaVersion.
static_assert(kSchemaVersion == 1, "update the links_schema message");
constexpr std::array<ErrorEntry, 10> kErrors{{
    {.code = "hwloc_load", .message = "hwloc could not load the topology of this machine"},
    {.code = "xml_reimport",
     .message = "the rewritten hwloc.xml does not load with the replay flags (RFC-0003 §2.1)"},
    {.code = "links_read", .message = "the --links file cannot be read or is not JSON"},
    {.code = "links_schema",
     .message = "the --links file does not match the links.json schema (supported: 1)"},
    {.code = "schema", .message = "an output file does not match its schema; see diagnostics.txt"},
    {.code = "leak",
     .message = "the leak check found an identifier of this machine in an output file; see "
                "diagnostics.txt for the file, line and kind"},
    {.code = "output_unreadable",
     .message = "an output file could not be read back for the leak check"},
    {.code = "replay",
     .message = "the written files do not replay into a topology model; see diagnostics.txt"},
    {.code = "write_failed",
     .message = "an output file could not be written (disk full or permission denied)"},
    {.code = "internal", .message = "unexpected internal error; see diagnostics.txt"},
}};

std::string_view leak_name(LeakCheck leak) {
    switch (leak) {
    case LeakCheck::passed:
        return "passed";
    case LeakCheck::failed:
        return "failed";
    case LeakCheck::not_run:
        break;
    }
    return "not_run";
}

[[noreturn]] void throw_errno(const char* what) {
    throw std::system_error(errno, std::generic_category(), what);
}

// Owns a POSIX descriptor so every throw path closes it.
class Fd {
  public:
    explicit Fd(int fd) : fd_(fd) {}
    Fd(const Fd&) = delete;
    Fd& operator=(const Fd&) = delete;
    Fd(Fd&&) = delete;
    Fd& operator=(Fd&&) = delete;
    ~Fd() {
        if (fd_ >= 0) {
            ::close(fd_);
        }
    }
    [[nodiscard]] int get() const { return fd_; }
    // close can report a deferred write error (NFS), so a successful write path checks it.
    void close_checked() {
        const int fd = std::exchange(fd_, -1);
        if (::close(fd) != 0) {
            throw_errno("close");
        }
    }

  private:
    int fd_;
};

void fsync_dir(const fs::path& dir) {
    const Fd fd(::open(dir.c_str(), O_RDONLY | O_DIRECTORY | O_CLOEXEC));
    if (fd.get() < 0) {
        throw_errno("open directory");
    }
    if (::fsync(fd.get()) != 0) {
        throw_errno("fsync directory");
    }
}

} // namespace

std::string_view to_string(Status status) {
    switch (status) {
    case Status::complete:
        return "complete";
    case Status::partial:
        return "partial";
    case Status::failed:
        break;
    }
    return "failed";
}

std::string_view error_message(std::string_view code) {
    for (const ErrorEntry& entry : kErrors) {
        if (entry.code == code) {
            return entry.message;
        }
    }
    throw std::out_of_range("no message for capture error code");
}

json manifest_json(const Manifest& manifest) {
    json files = json::object();
    for (const auto& [name, hash] : manifest.files) {
        files[name] = hash;
    }
    json missing = json::array();
    for (const MissingFile& m : manifest.missing) {
        missing.push_back({{"file", m.file}, {"reason", m.reason}});
    }
    json errors = json::array();
    for (const std::string& code : manifest.errors) {
        errors.push_back({{"code", code}, {"message", error_message(code)}});
    }
    return {{"schema", kSchemaVersion},
            {"tool_version", OSTIA_TOOL_VERSION},
            {"status", to_string(manifest.status)},
            {"sanitized", manifest.sanitized},
            {"leak_check", leak_name(manifest.leak_check)},
            {"topology_id", manifest.topology_id ? json(*manifest.topology_id) : json(nullptr)},
            {"files", std::move(files)},
            {"missing", std::move(missing)},
            {"errors", std::move(errors)}};
}

std::string file_hash(std::string_view bytes) {
    std::string out = "sha256:";
    out += sha256_hex(bytes);
    return out;
}

int exit_for(bool leak_or_schema, bool other_failure, bool partial) {
    if (leak_or_schema) {
        return 3;
    }
    if (other_failure) {
        return 1;
    }
    return partial ? 2 : 0;
}

namespace {

// O_NOFOLLOW: a symlink planted at an output name must not redirect the write elsewhere.
void write_synced(const fs::path& path, std::string_view bytes, int create_flags) {
    // 0644: the capture is meant to be fetched and published; nothing in it is secret.
    constexpr mode_t kMode = 0644;
    Fd fd(::open(path.c_str(), O_WRONLY | O_CREAT | O_NOFOLLOW | O_CLOEXEC | create_flags, kMode));
    if (fd.get() < 0) {
        throw_errno("open");
    }
    while (!bytes.empty()) {
        const ssize_t n = ::write(fd.get(), bytes.data(), bytes.size());
        if (n < 0 && errno == EINTR) {
            continue;
        }
        if (n <= 0) {
            throw_errno("write");
        }
        bytes.remove_prefix(static_cast<std::size_t>(n));
    }
    if (::fsync(fd.get()) != 0) {
        throw_errno("fsync");
    }
    fd.close_checked();
}

} // namespace

void write_file_synced(const fs::path& path, std::string_view bytes) {
    write_synced(path, bytes, O_TRUNC);
}

void write_manifest_atomic(const fs::path& out, const json& manifest) {
    const fs::path tmp = out / "manifest.json.tmp";
    try {
        // A leftover from a killed run is removed, and O_EXCL then guarantees the file renamed
        // into place is the one written here.
        std::error_code ec;
        fs::remove(tmp, ec);
        write_synced(tmp, dump(manifest), O_EXCL);
        // The data files' directory entries are durable before a manifest can list them.
        fsync_dir(out);
        if (::rename(tmp.c_str(), (out / "manifest.json").c_str()) != 0) {
            throw_errno("rename");
        }
    } catch (...) {
        std::error_code ec;
        fs::remove(tmp, ec);
        throw;
    }
    // The rename is durable only once the directory entry is.
    fsync_dir(out);
}

} // namespace ostia::fabric::topology::capture
