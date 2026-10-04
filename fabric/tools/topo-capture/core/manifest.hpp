#pragma once

#include <filesystem>
#include <map>
#include <nlohmann/json.hpp>
#include <optional>
#include <string>
#include <string_view>
#include <vector>

namespace ostia::fabric::topology::capture {

enum class Status { complete, partial, failed };
enum class LeakCheck { not_run, passed, failed };

std::string_view to_string(Status status);

struct MissingFile {
    std::string file, reason;
};

// RFC-0003 §4. Errors are codes only: manifest_json takes each message from a fixed table, so no
// message can carry a value from the machine.
struct Manifest {
    Status status = Status::failed;
    bool sanitized = false;
    LeakCheck leak_check = LeakCheck::not_run;
    std::optional<std::string> topology_id;
    std::map<std::string, std::string> files; // name -> "sha256:<64 hex>"
    std::vector<MissingFile> missing;
    std::vector<std::string> errors;
};

// The fixed message for an error code; an unknown code is a programming error and throws
// std::out_of_range, so a new code cannot ship without its message.
std::string_view error_message(std::string_view code);

nlohmann::json manifest_json(const Manifest& manifest);

// "sha256:<64 hex>" of the bytes exactly as written.
std::string file_hash(std::string_view bytes);

// RFC-0003 §4 precedence: 3 (leak or schema) > 1 (other failure) > 2 (partial) > 0.
int exit_for(bool leak_or_schema, bool other_failure, bool partial);

// Writes, fsyncs and closes path; throws std::system_error on any failure (a full disk is the
// usual one), leaving a partial file for the caller to remove.
void write_file_synced(const std::filesystem::path& path, std::string_view bytes);

// manifest.json in out, written last: a temporary file, fsync, fsync of the directory, rename,
// then fsync of the directory again, so a reader sees the old manifest, the new one or none, never
// a partial one. Throws std::system_error; the temporary file is removed on failure.
void write_manifest_atomic(const std::filesystem::path& out, const nlohmann::json& manifest);

} // namespace ostia::fabric::topology::capture
