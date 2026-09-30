// Shared pieces of the M0 gate workload programs (RFC-0001 §6.4): arguments, the data
// pattern every program checksums, result output in the `ostia` format that
// tools/bench/ostia_bench.py completes to schema 1, and `evidence:` lines that
// tools/bench/evidence.py reads. These are standalone reference programs, not Ostia's
// data path; they port the prototype's ideas, not its code (D6).
#pragma once

#include <algorithm>
#include <chrono>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <map>
#include <string>
#include <vector>

#if defined(__CUDACC__)
#define OSTIA_BENCH_HD __host__ __device__
#else
#define OSTIA_BENCH_HD
#endif

namespace ostia::bench {

inline constexpr int kSkip = 77; // ctest SKIP_RETURN_CODE: no CUDA device
inline constexpr std::size_t kMiB = std::size_t{1} << 20;
inline constexpr std::size_t kGiB = std::size_t{1} << 30;

// --key value and --flag arguments; sizes accept KiB, MiB and GiB suffixes.
class Args {
  public:
    Args(int argc, char** argv) {
        for (int i = 1; i < argc; ++i) {
            std::string key = argv[i];
            if (key.rfind("--", 0) != 0) {
                continue;
            }
            key = key.substr(2);
            const bool has_value = i + 1 < argc && std::string(argv[i + 1]).rfind("--", 0) != 0;
            values_[key] = has_value ? argv[++i] : "";
        }
    }
    [[nodiscard]] bool flag(const std::string& key) const { return values_.count(key) != 0; }
    [[nodiscard]] std::string str(const std::string& key, const std::string& fallback) const {
        auto it = values_.find(key);
        return it == values_.end() ? fallback : it->second;
    }
    [[nodiscard]] long num(const std::string& key, long fallback) const {
        auto it = values_.find(key);
        return it == values_.end() ? fallback : std::stol(it->second);
    }
    [[nodiscard]] std::size_t size(const std::string& key, std::size_t fallback) const {
        auto it = values_.find(key);
        return it == values_.end() ? fallback : parse_size(it->second);
    }
    static std::size_t parse_size(const std::string& text) {
        std::size_t pos = 0;
        const auto n = std::stoull(text, &pos);
        const std::string unit = text.substr(pos);
        if (unit == "KiB") {
            return n << 10;
        }
        if (unit == "MiB") {
            return n << 20;
        }
        if (unit == "GiB") {
            return n << 30;
        }
        return n;
    }

  private:
    std::map<std::string, std::string> values_;
};

// Samples per measurement: --samples, else OSTIA_BENCH_RUNS from ostia_bench.py, else 10.
// --smoke runs 3 samples of a small size and checks checksums only (one L4 is enough).
inline int samples(const Args& a) {
    if (a.flag("smoke")) {
        return 3;
    }
    const char* env = std::getenv("OSTIA_BENCH_RUNS");
    return static_cast<int>(a.num("samples", env != nullptr ? std::atol(env) : 10));
}

// The byte every program writes at offset i; checksums compare against it.
OSTIA_BENCH_HD inline std::uint8_t pattern(std::uint32_t seed, std::uint64_t i) {
    return static_cast<std::uint8_t>((i * 7 + seed * 31 + (i >> 12)) & 0xffU);
}

inline std::uint64_t count_mismatches(const std::uint8_t* data, std::uint64_t n, std::uint32_t seed,
                                      std::uint64_t offset = 0) {
    std::uint64_t bad = 0;
    for (std::uint64_t i = 0; i < n; ++i) {
        bad += data[i] != pattern(seed, offset + i) ? 1 : 0;
    }
    return bad;
}

inline double gbps(std::uint64_t bytes, double seconds) {
    return static_cast<double>(bytes) / seconds / 1e9;
}

class Timer {
  public:
    Timer() : start_(std::chrono::steady_clock::now()) {}
    [[nodiscard]] double seconds() const {
        return std::chrono::duration<double>(std::chrono::steady_clock::now() - start_).count();
    }

  private:
    std::chrono::steady_clock::time_point start_;
};

// One result record: {"bench", "params", "unit", "higher_is_better", "samples"}.
inline void emit(const std::string& bench, const std::string& params_json,
                 const std::vector<double>& values) {
    std::string s;
    for (std::size_t i = 0; i < values.size(); ++i) {
        char buf[32];
        std::snprintf(buf, sizeof(buf), "%s%.6g", i == 0 ? "" : ", ", values[i]);
        s += buf;
    }
    std::printf("{\"bench\": \"%s\", \"params\": %s, \"unit\": \"GB/s\", "
                "\"higher_is_better\": true, \"samples\": [%s]}\n",
                bench.c_str(), params_json.c_str(), s.c_str());
    std::fflush(stdout);
}

inline void evidence(const std::string& facts) {
    std::printf("evidence: %s\n", facts.c_str());
    std::fflush(stdout);
}

// The error-message contract (RFC-0001, Failure handling).
inline int fail(const std::string& problem, const std::string& fix) {
    std::fprintf(stderr, "error: %s\n  fix: %s\n  see: RFC-0001 §6.4\n", problem.c_str(),
                 fix.c_str());
    return 1;
}

inline int checksum_failed(const std::string& bench, std::uint64_t bad, std::uint64_t n) {
    std::fprintf(stderr,
                 "error: %s: %llu of %llu received bytes failed their checksum\n"
                 "  rule: a gate workload's received data must match what was sent\n"
                 "  see: RFC-0001 §6.4\n",
                 bench.c_str(), static_cast<unsigned long long>(bad),
                 static_cast<unsigned long long>(n));
    return 1;
}

} // namespace ostia::bench
