#pragma once

#include <cstddef>
#include <filesystem>

namespace ostia::fabric::topology::capture {

// Removes the scratch directory and partial data files on SIGINT or SIGTERM (SIGKILL cannot be
// covered, hence the scratch directory under $TMPDIR). Process-global: a handler has no context.
class CleanupScope {
  public:
    // The outermost scope installs the handlers and restores the previous ones; paths tracked
    // inside a scope are dropped when it ends, as the pipeline has dealt with them by then.
    CleanupScope();
    CleanupScope(const CleanupScope&) = delete;
    CleanupScope& operator=(const CleanupScope&) = delete;
    CleanupScope(CleanupScope&&) = delete;
    CleanupScope& operator=(CleanupScope&&) = delete;
    ~CleanupScope();

    // Drops the paths tracked inside this scope once the capture they belong to is final, so a
    // late signal cannot unlink files a complete manifest lists.
    void release() const;

  private:
    std::size_t mark_;
};

// Call before creating a file; a mkdtemp directory is tracked right after, as its name is known
// only then. False when the list is full or the path too long: only a signal would then miss it.
bool track_for_cleanup(const std::filesystem::path& path, bool directory);

// Async-signal-safe (unlink and rmdir only), newest path first.
void run_signal_cleanup() noexcept;

} // namespace ostia::fabric::topology::capture
