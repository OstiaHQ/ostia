#include "core/cleanup.hpp"

#include <array>
#include <atomic>
#include <csignal>
#include <string>
#include <unistd.h>

namespace ostia::fabric::topology::capture {

namespace {

constexpr std::size_t kMaxTracked = 64;
constexpr std::size_t kMaxPath = 4096;

struct Tracked {
    std::array<char, kMaxPath> path;
    bool directory;
};

// An entry is complete before the count that publishes it is stored, so the handler reads only
// finished entries; a lock-free atomic is what makes that load async-signal-safe.
std::array<Tracked, kMaxTracked> tracked{};
std::atomic<std::size_t> tracked_count{0};
static_assert(std::atomic<std::size_t>::is_always_lock_free);

int scope_depth = 0;
struct sigaction previous_int{};
struct sigaction previous_term{};

void on_signal(int /*signal*/) {
    run_signal_cleanup();
    ::_exit(1);
}

} // namespace

CleanupScope::CleanupScope() : mark_(tracked_count.load()) {
    if (scope_depth++ == 0) {
        struct sigaction action{};
        action.sa_handler = on_signal;
        sigemptyset(&action.sa_mask);
        sigaction(SIGINT, &action, &previous_int);
        sigaction(SIGTERM, &action, &previous_term);
    }
}

CleanupScope::~CleanupScope() {
    tracked_count.store(mark_);
    if (--scope_depth == 0) {
        sigaction(SIGINT, &previous_int, nullptr);
        sigaction(SIGTERM, &previous_term, nullptr);
    }
}

bool track_for_cleanup(const std::filesystem::path& path, bool directory) {
    const std::size_t n = tracked_count.load();
    const std::string& text = path.native();
    if (n >= kMaxTracked || text.size() >= kMaxPath) {
        return false;
    }
    Tracked& entry = tracked.at(n);
    text.copy(entry.path.data(), text.size());
    entry.path.at(text.size()) = '\0';
    entry.directory = directory;
    tracked_count.store(n + 1);
    return true;
}

void run_signal_cleanup() noexcept {
    for (std::size_t i = tracked_count.load(); i > 0; --i) {
        const Tracked& entry = tracked[i - 1];
        if (entry.directory) {
            ::rmdir(entry.path.data());
        } else {
            ::unlink(entry.path.data());
        }
    }
}

} // namespace ostia::fabric::topology::capture
