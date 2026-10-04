#pragma once

#include <optional>
#include <stdexcept>
#include <string>
#include <utility>
#include <vector>

#include "core/apis.hpp"
#include "core/result.hpp"

namespace ostia::fabric::topology::capture::fakes {

class FakeNvml final : public NvmlApi {
  public:
    explicit FakeNvml(NvmlFacts facts) : facts_(std::move(facts)) {}

    static FakeNvml failing(std::string code) {
        FakeNvml fake{NvmlFacts{}};
        fake.error_ = std::move(code);
        return fake;
    }
    static FakeNvml throwing() {
        FakeNvml fake{NvmlFacts{}};
        fake.throws_ = true;
        return fake;
    }

    Result<NvmlFacts> query() override {
        ++calls_;
        if (throws_) {
            throw std::runtime_error("fake NVML failure");
        }
        if (error_) {
            return Result<NvmlFacts>::failure(*error_);
        }
        return facts_;
    }

    [[nodiscard]] int calls() const { return calls_; }

  private:
    NvmlFacts facts_;
    std::optional<std::string> error_;
    bool throws_ = false;
    int calls_ = 0;
};

// nullopt: the probe could not run; an empty list: it ran and found nothing.
class FakeVerbs final : public VerbsApi {
  public:
    explicit FakeVerbs(std::optional<std::vector<VerbsPort>> ports) : ports_(std::move(ports)) {}
    std::optional<std::vector<VerbsPort>> ports() override { return ports_; }

  private:
    std::optional<std::vector<VerbsPort>> ports_;
};

} // namespace ostia::fabric::topology::capture::fakes
