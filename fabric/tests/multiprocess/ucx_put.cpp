// Multi-process harness test (RFC-0001 §4.1): rank 0 exposes a buffer, every other rank
// writes a checksummed slice into it with a raw UCX put, rank 0 verifies. The ranks
// rendezvous through files in a shared directory. Replaced by fabric's own multi-process
// tests in M1.
#include <ucp/api/ucp.h>

#include <chrono>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <filesystem>
#include <fstream>
#include <string>
#include <thread>
#include <vector>

namespace fs = std::filesystem;

namespace {

struct Args {
    int rank = -1;
    int size = 2;
    fs::path dir;
    std::size_t bytes = 1 << 20;
    bool corrupt = false;
};

constexpr auto kTimeout = std::chrono::seconds(60);

std::uint8_t pattern(int rank, std::size_t i) {
    return static_cast<std::uint8_t>(rank * 31 + i * 7);
}

bool check(ucs_status_t s, const char* what) {
    if (s != UCS_OK) {
        std::fprintf(stderr, "error: %s failed: %s\n", what, ucs_status_string(s));
        return false;
    }
    return true;
}

void write_file(const fs::path& path, const std::string& data) {
    const fs::path tmp = path.string() + ".tmp";
    std::ofstream(tmp, std::ios::binary).write(data.data(), static_cast<std::streamsize>(data.size()));
    fs::rename(tmp, path); // atomic: readers never see a partial file
}

bool wait_for(const fs::path& path, ucp_worker_h worker) {
    const auto deadline = std::chrono::steady_clock::now() + kTimeout;
    while (!fs::exists(path)) {
        if (worker != nullptr) {
            ucp_worker_progress(worker);
        } else {
            std::this_thread::sleep_for(std::chrono::milliseconds(1));
        }
        if (std::chrono::steady_clock::now() > deadline) {
            std::fprintf(stderr, "error: timed out waiting for %s\n", path.c_str());
            return false;
        }
    }
    return true;
}

bool wait_request(ucp_worker_h worker, ucs_status_ptr_t request, const char* what) {
    if (request == nullptr) {
        return true;
    }
    if (UCS_PTR_IS_ERR(request)) {
        return check(UCS_PTR_STATUS(request), what);
    }
    ucs_status_t s;
    do {
        ucp_worker_progress(worker);
        s = ucp_request_check_status(request);
    } while (s == UCS_INPROGRESS);
    ucp_request_free(request);
    return check(s, what);
}

void put_blob(std::string& out, const void* data, std::uint64_t len) {
    out.append(reinterpret_cast<const char*>(&len), sizeof(len));
    out.append(static_cast<const char*>(data), len);
}

std::string get_blob(const std::string& in, std::size_t& pos) {
    std::uint64_t len = 0;
    std::memcpy(&len, in.data() + pos, sizeof(len));
    pos += sizeof(len);
    std::string blob = in.substr(pos, len);
    pos += len;
    return blob;
}

int target(const Args& a, ucp_context_h ctx, ucp_worker_h worker, const std::string& address) {
    const std::size_t total = a.bytes * static_cast<std::size_t>(a.size - 1);
    std::vector<std::uint8_t> buffer(total, 0);
    ucp_mem_map_params_t mp{};
    mp.field_mask = UCP_MEM_MAP_PARAM_FIELD_ADDRESS | UCP_MEM_MAP_PARAM_FIELD_LENGTH;
    mp.address = buffer.data();
    mp.length = total;
    ucp_mem_h memh = nullptr;
    if (!check(ucp_mem_map(ctx, &mp, &memh), "ucp_mem_map")) {
        return 1;
    }
    void* rkey = nullptr;
    std::size_t rkey_len = 0;
    if (!check(ucp_rkey_pack(ctx, memh, &rkey, &rkey_len), "ucp_rkey_pack")) {
        return 1;
    }
    std::string info;
    put_blob(info, address.data(), address.size());
    put_blob(info, rkey, rkey_len);
    const auto base = reinterpret_cast<std::uint64_t>(buffer.data());
    info.append(reinterpret_cast<const char*>(&base), sizeof(base));
    ucp_rkey_buffer_release(rkey);
    write_file(a.dir / "rank0.info", info);

    for (int r = 1; r < a.size; ++r) {
        if (!wait_for(a.dir / ("done." + std::to_string(r)), worker)) {
            return 1;
        }
    }
    if (a.corrupt) {
        buffer[total / 2] ^= 0xff; // self-check: the verifier must notice this
    }
    int bad = 0;
    for (int r = 1; r < a.size; ++r) {
        for (std::size_t i = 0; i < a.bytes; ++i) {
            if (buffer[(r - 1) * a.bytes + i] != pattern(r, i)) {
                ++bad;
                break;
            }
        }
    }
    write_file(a.dir / "finish", "");
    ucp_mem_unmap(ctx, memh);
    if (bad != 0) {
        std::fprintf(stderr, "error: %d of %d slices failed their checksum\n", bad, a.size - 1);
        return 1;
    }
    std::printf("ok: %d ranks put %zu bytes each, all checksums match\n", a.size - 1, a.bytes);
    return 0;
}

int source(const Args& a, ucp_worker_h worker) {
    if (!wait_for(a.dir / "rank0.info", nullptr)) {
        return 1;
    }
    std::ifstream f(a.dir / "rank0.info", std::ios::binary);
    const std::string info((std::istreambuf_iterator<char>(f)), std::istreambuf_iterator<char>());
    std::size_t pos = 0;
    const std::string address = get_blob(info, pos);
    const std::string rkey_buf = get_blob(info, pos);
    std::uint64_t base = 0;
    std::memcpy(&base, info.data() + pos, sizeof(base));

    ucp_ep_params_t ep_params{};
    ep_params.field_mask = UCP_EP_PARAM_FIELD_REMOTE_ADDRESS;
    ep_params.address = reinterpret_cast<const ucp_address_t*>(address.data());
    ucp_ep_h ep = nullptr;
    if (!check(ucp_ep_create(worker, &ep_params, &ep), "ucp_ep_create")) {
        return 1;
    }
    ucp_rkey_h rkey = nullptr;
    if (!check(ucp_ep_rkey_unpack(ep, rkey_buf.data(), &rkey), "ucp_ep_rkey_unpack")) {
        return 1;
    }
    std::vector<std::uint8_t> data(a.bytes);
    for (std::size_t i = 0; i < a.bytes; ++i) {
        data[i] = pattern(a.rank, i);
    }
    ucp_request_param_t rp{};
    const std::uint64_t dest = base + static_cast<std::uint64_t>(a.rank - 1) * a.bytes;
    if (!wait_request(worker, ucp_put_nbx(ep, data.data(), a.bytes, dest, rkey, &rp), "ucp_put_nbx") ||
        !wait_request(worker, ucp_ep_flush_nbx(ep, &rp), "ucp_ep_flush_nbx")) {
        return 1;
    }
    if (a.rank == 1) {
        ucp_ep_print_info(ep, stdout); // transport evidence for the launcher
        std::fflush(stdout);
    }
    write_file(a.dir / ("done." + std::to_string(a.rank)), "");
    if (!wait_for(a.dir / "finish", worker)) {
        return 1;
    }
    ucp_rkey_destroy(rkey);
    ucp_request_param_t cp{};
    cp.op_attr_mask = UCP_OP_ATTR_FIELD_FLAGS;
    cp.flags = UCP_EP_CLOSE_FLAG_FORCE;
    wait_request(worker, ucp_ep_close_nbx(ep, &cp), "ucp_ep_close_nbx");
    return 0;
}

} // namespace

int main(int argc, char** argv) {
    Args a;
    for (int i = 1; i < argc; ++i) {
        const std::string arg = argv[i];
        auto next = [&] { return std::string(i + 1 < argc ? argv[++i] : ""); };
        if (arg == "--rank") {
            a.rank = std::stoi(next());
        } else if (arg == "--size") {
            a.size = std::stoi(next());
        } else if (arg == "--dir") {
            a.dir = next();
        } else if (arg == "--bytes") {
            a.bytes = std::stoull(next());
        } else if (arg == "--corrupt") {
            a.corrupt = true;
        }
    }
    if (a.rank < 0 || a.size < 2 || a.dir.empty()) {
        std::fprintf(stderr, "usage: ucx_put --rank R --size N --dir DIR [--bytes B] [--corrupt]\n");
        return 2;
    }
    ucp_params_t params{};
    params.field_mask = UCP_PARAM_FIELD_FEATURES;
    params.features = UCP_FEATURE_RMA;
    ucp_context_h ctx = nullptr;
    if (!check(ucp_init(&params, nullptr, &ctx), "ucp_init")) {
        return 1;
    }
    ucp_worker_params_t wp{};
    wp.field_mask = UCP_WORKER_PARAM_FIELD_THREAD_MODE;
    wp.thread_mode = UCS_THREAD_MODE_SINGLE;
    ucp_worker_h worker = nullptr;
    if (!check(ucp_worker_create(ctx, &wp, &worker), "ucp_worker_create")) {
        return 1;
    }
    ucp_address_t* address = nullptr;
    std::size_t address_len = 0;
    if (!check(ucp_worker_get_address(worker, &address, &address_len), "ucp_worker_get_address")) {
        return 1;
    }
    const std::string addr(reinterpret_cast<const char*>(address), address_len);
    ucp_worker_release_address(worker, address);
    const int rc = a.rank == 0 ? target(a, ctx, worker, addr) : source(a, worker);
    ucp_worker_destroy(worker);
    ucp_cleanup(ctx);
    return rc;
}
