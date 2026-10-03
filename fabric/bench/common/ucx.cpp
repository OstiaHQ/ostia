#include "ucx.hpp"

#include <arpa/inet.h>
#include <cerrno>
#include <chrono>
#include <cstring>
#include <filesystem>
#include <fstream>
#include <netdb.h>
#include <netinet/in.h>
#include <poll.h>
#include <sys/socket.h>
#include <thread>
#include <ucp/api/ucp.h>
#include <unistd.h>
#ifdef OSTIA_BENCH_CUDA
#include <cuda_runtime.h>
#endif

namespace ostia::bench::ucx {
namespace {

namespace fs = std::filesystem;
using Clock = std::chrono::steady_clock;
constexpr auto kConnectTimeout = std::chrono::seconds(60);
constexpr auto kCommandTimeout = std::chrono::seconds(900);

bool ok(ucs_status_t s, const char* what) {
    if (s != UCS_OK) {
        std::fprintf(stderr, "error: %s failed: %s\n  see: RFC-0001 §6.4\n", what,
                     ucs_status_string(s));
        return false;
    }
    return true;
}

void put_u64(std::string& out, std::uint64_t v) {
    out.append(reinterpret_cast<const char*>(&v), sizeof(v));
}
void put_str(std::string& out, const std::string& s) {
    put_u64(out, s.size());
    out += s;
}
struct Reader {
    const std::string& in;
    std::size_t pos = 0;
    std::uint64_t u64() {
        std::uint64_t v = 0;
        std::memcpy(&v, in.data() + pos, sizeof(v));
        pos += sizeof(v);
        return v;
    }
    std::string str() {
        const auto n = u64();
        std::string s = in.substr(pos, n);
        pos += n;
        return s;
    }
};

class Channel {
  public:
    Channel() = default;
    Channel(const Channel&) = delete;
    Channel& operator=(const Channel&) = delete;
    ~Channel() {
        if (fd_ >= 0) {
            ::close(fd_);
        }
    }

    bool open(const Args& a, int rank) { return rank == 0 ? listen(a) : connect(a); }

    bool send(const std::string& blob) {
        std::string framed;
        put_str(framed, blob);
        std::size_t done = 0;
        while (done < framed.size()) {
            const auto n = ::send(fd_, framed.data() + done, framed.size() - done, 0);
            if (n <= 0) {
                std::perror("error: rendezvous send");
                return false;
            }
            done += static_cast<std::size_t>(n);
        }
        return true;
    }

    // Waits for the next message, calling progress() while none is readable, so that
    // transports that need the target to progress (TCP) keep moving.
    bool recv(std::string& blob, const std::function<void()>& progress) {
        const auto deadline = Clock::now() + kCommandTimeout;
        std::uint64_t len = 0;
        if (!read_exact(reinterpret_cast<char*>(&len), sizeof(len), progress, deadline)) {
            return false;
        }
        blob.assign(len, '\0');
        return read_exact(blob.data(), len, progress, deadline);
    }

  private:
    bool read_exact(char* out, std::size_t n, const std::function<void()>& progress,
                    Clock::time_point deadline) {
        std::size_t done = 0;
        while (done < n) {
            pollfd p{fd_, POLLIN, 0};
            if (::poll(&p, 1, 0) == 0) {
                progress();
                if (Clock::now() > deadline) {
                    std::fprintf(stderr, "error: rendezvous timed out\n  see: RFC-0001 §6.4\n");
                    return false;
                }
                continue;
            }
            const auto r = ::recv(fd_, out + done, n - done, 0);
            if (r <= 0) {
                std::fprintf(stderr, "error: the peer closed the rendezvous connection\n");
                return false;
            }
            done += static_cast<std::size_t>(r);
        }
        return true;
    }

    bool listen(const Args& a) {
        const bool local = a.flag("dir");
        const int port = local ? 0 : static_cast<int>(a.num("listen", 0));
        const int s = ::socket(AF_INET, SOCK_STREAM, 0);
        const int one = 1;
        ::setsockopt(s, SOL_SOCKET, SO_REUSEADDR, &one, sizeof(one));
        sockaddr_in addr{};
        addr.sin_family = AF_INET;
        addr.sin_port = htons(static_cast<std::uint16_t>(port));
        addr.sin_addr.s_addr = htonl(local ? INADDR_LOOPBACK : INADDR_ANY);
        if (::bind(s, reinterpret_cast<sockaddr*>(&addr), sizeof(addr)) != 0 ||
            ::listen(s, 1) != 0) {
            std::perror("error: rendezvous listen");
            ::close(s);
            return false;
        }
        if (local) {
            socklen_t len = sizeof(addr);
            ::getsockname(s, reinterpret_cast<sockaddr*>(&addr), &len);
            const fs::path dir = a.str("dir", ".");
            const fs::path tmp = dir / "port.tmp";
            std::ofstream(tmp) << ntohs(addr.sin_port) << "\n";
            fs::rename(tmp, dir / "port"); // atomic: the peer never reads a partial file
        }
        pollfd p{s, POLLIN, 0};
        if (::poll(&p, 1, static_cast<int>(kConnectTimeout.count() * 1000)) != 1) {
            std::fprintf(stderr, "error: no peer connected within %llds\n",
                         static_cast<long long>(kConnectTimeout.count()));
            ::close(s);
            return false;
        }
        fd_ = ::accept(s, nullptr, nullptr);
        ::close(s);
        return fd_ >= 0;
    }

    bool connect(const Args& a) {
        std::string host = "127.0.0.1";
        std::string port;
        const auto deadline = Clock::now() + kConnectTimeout;
        if (a.flag("dir")) {
            const fs::path file = fs::path(a.str("dir", ".")) / "port";
            while (!fs::exists(file)) {
                if (Clock::now() > deadline) {
                    std::fprintf(stderr, "error: rank 0 did not publish %s\n", file.c_str());
                    return false;
                }
                std::this_thread::sleep_for(std::chrono::milliseconds(5));
            }
            std::ifstream(file) >> port;
        } else {
            const std::string target = a.str("connect", "");
            const auto colon = target.rfind(':');
            if (colon == std::string::npos) {
                std::fprintf(stderr, "error: --connect takes HOST:PORT\n");
                return false;
            }
            host = target.substr(0, colon);
            port = target.substr(colon + 1);
        }
        addrinfo hints{};
        hints.ai_family = AF_INET;
        hints.ai_socktype = SOCK_STREAM;
        addrinfo* res = nullptr;
        if (::getaddrinfo(host.c_str(), port.c_str(), &hints, &res) != 0 || res == nullptr) {
            std::fprintf(stderr, "error: cannot resolve %s\n", host.c_str());
            return false;
        }
        while (true) {
            fd_ = ::socket(res->ai_family, res->ai_socktype, res->ai_protocol);
            if (::connect(fd_, res->ai_addr, res->ai_addrlen) == 0) {
                break;
            }
            ::close(fd_);
            fd_ = -1;
            if (Clock::now() > deadline) {
                std::fprintf(stderr, "error: cannot connect to %s:%s\n", host.c_str(),
                             port.c_str());
                ::freeaddrinfo(res);
                return false;
            }
            std::this_thread::sleep_for(std::chrono::milliseconds(50));
        }
        ::freeaddrinfo(res);
        return true;
    }

    int fd_ = -1;
};

// Host or CUDA memory; fill and checksum go through a host copy for CUDA memory.
class Memory {
  public:
    Memory(std::uint64_t bytes, bool cuda) : bytes_(bytes), cuda_(cuda) {
#ifdef OSTIA_BENCH_CUDA
        if (cuda_) {
            if (cudaMalloc(reinterpret_cast<void**>(&data_), bytes) != cudaSuccess) {
                data_ = nullptr;
            }
            return;
        }
#endif
        host_.assign(bytes, 0);
        data_ = host_.data();
    }
    ~Memory() {
#ifdef OSTIA_BENCH_CUDA
        if (cuda_ && data_ != nullptr) {
            cudaFree(data_);
        }
#endif
    }
    Memory(const Memory&) = delete;
    Memory& operator=(const Memory&) = delete;

    [[nodiscard]] std::uint8_t* data() const { return data_; }
    [[nodiscard]] std::uint64_t bytes() const { return bytes_; }

    void write(const std::vector<std::uint8_t>& from) {
#ifdef OSTIA_BENCH_CUDA
        if (cuda_) {
            cudaMemcpy(data_, from.data(), from.size(), cudaMemcpyHostToDevice);
            return;
        }
#endif
        std::memcpy(data_, from.data(), from.size());
    }
    [[nodiscard]] std::vector<std::uint8_t> read() const {
        std::vector<std::uint8_t> out(bytes_);
#ifdef OSTIA_BENCH_CUDA
        if (cuda_) {
            cudaMemcpy(out.data(), data_, bytes_, cudaMemcpyDeviceToHost);
            return out;
        }
#endif
        std::memcpy(out.data(), data_, bytes_);
        return out;
    }
    void fill(std::uint32_t seed) {
        std::vector<std::uint8_t> host(bytes_);
        for (std::uint64_t i = 0; i < bytes_; ++i) {
            host[i] = pattern(seed, i);
        }
        write(host);
    }
    void zero() { write(std::vector<std::uint8_t>(bytes_, 0)); }

  private:
    std::uint64_t bytes_;
    bool cuda_;
    std::uint8_t* data_ = nullptr;
    std::vector<std::uint8_t> host_;
};

// One UCP context and worker, optionally restricted to transports and devices; on the
// source, also the endpoint to the target and the target's rkey.
class Session {
  public:
    Session(const std::string& tls, const std::string& devices) {
        ucp_config_t* config = nullptr;
        if (!ok(ucp_config_read(nullptr, nullptr, &config), "ucp_config_read")) {
            return;
        }
        if (!tls.empty()) {
            ucp_config_modify(config, "TLS", tls.c_str());
        }
        if (!devices.empty()) {
            ucp_config_modify(config, "NET_DEVICES", devices.c_str());
        }
        ucp_params_t params{};
        params.field_mask = UCP_PARAM_FIELD_FEATURES;
        params.features = UCP_FEATURE_RMA;
        const bool inited = ok(ucp_init(&params, config, &ctx_), "ucp_init");
        ucp_config_release(config);
        if (!inited) {
            return;
        }
        ucp_worker_params_t wp{};
        wp.field_mask = UCP_WORKER_PARAM_FIELD_THREAD_MODE;
        wp.thread_mode = UCS_THREAD_MODE_SINGLE;
        ready_ = ok(ucp_worker_create(ctx_, &wp, &worker_), "ucp_worker_create");
    }
    ~Session() {
        close();
        for (auto* m : maps_) {
            ucp_mem_unmap(ctx_, m);
        }
        if (worker_ != nullptr) {
            ucp_worker_destroy(worker_);
        }
        if (ctx_ != nullptr) {
            ucp_cleanup(ctx_);
        }
    }
    Session(const Session&) = delete;
    Session& operator=(const Session&) = delete;

    [[nodiscard]] bool ready() const { return ready_; }

    // Closes the endpoint gracefully (a forced close needs UCP_ERR_HANDLING_MODE_PEER on
    // both sides), so the source calls it while the target still progresses its worker,
    // before telling the target to quit.
    void close() {
        if (rkey_ != nullptr) {
            ucp_rkey_destroy(rkey_);
            rkey_ = nullptr;
        }
        if (ep_ != nullptr) {
            ucp_request_param_t p{};
            wait(ucp_ep_close_nbx(ep_, &p), "ucp_ep_close_nbx");
            ep_ = nullptr;
        }
    }
    void progress() { ucp_worker_progress(worker_); }

    std::string address() {
        ucp_address_t* a = nullptr;
        std::size_t n = 0;
        if (!ok(ucp_worker_get_address(worker_, &a, &n), "ucp_worker_get_address")) {
            return {};
        }
        std::string s(reinterpret_cast<const char*>(a), n);
        ucp_worker_release_address(worker_, a);
        return s;
    }

    ucp_mem_h map(const Memory& m) {
        ucp_mem_map_params_t mp{};
        mp.field_mask = UCP_MEM_MAP_PARAM_FIELD_ADDRESS | UCP_MEM_MAP_PARAM_FIELD_LENGTH;
        mp.address = m.data();
        mp.length = m.bytes();
        ucp_mem_h memh = nullptr;
        if (!ok(ucp_mem_map(ctx_, &mp, &memh), "ucp_mem_map")) {
            return nullptr;
        }
        maps_.push_back(memh);
        return memh;
    }

    std::string rkey(ucp_mem_h memh) {
        void* buf = nullptr;
        std::size_t n = 0;
        if (!ok(ucp_rkey_pack(ctx_, memh, &buf, &n), "ucp_rkey_pack")) {
            return {};
        }
        std::string s(static_cast<const char*>(buf), n);
        ucp_rkey_buffer_release(buf);
        return s;
    }

    // The memory type UCX registered: the transport evidence for GPU memory (§6.4).
    static std::string memory_type(ucp_mem_h memh) {
        ucp_mem_attr_t attr{};
        attr.field_mask = UCP_MEM_ATTR_FIELD_MEM_TYPE;
        if (ucp_mem_query(memh, &attr) != UCS_OK) {
            return "unknown";
        }
        switch (attr.mem_type) {
        case UCS_MEMORY_TYPE_HOST:
            return "host";
        case UCS_MEMORY_TYPE_CUDA:
            return "cuda";
        case UCS_MEMORY_TYPE_CUDA_MANAGED:
            return "cuda_managed";
        default:
            return "other";
        }
    }

    bool connect(const std::string& address, const std::string& rkey) {
        ucp_ep_params_t ep{};
        ep.field_mask = UCP_EP_PARAM_FIELD_REMOTE_ADDRESS;
        ep.address = reinterpret_cast<const ucp_address_t*>(address.data());
        return ok(ucp_ep_create(worker_, &ep, &ep_), "ucp_ep_create") &&
               ok(ucp_ep_rkey_unpack(ep_, rkey.data(), &rkey_), "ucp_ep_rkey_unpack");
    }

    ucs_status_ptr_t put(const std::uint8_t* local, std::uint64_t n, std::uint64_t remote,
                         ucp_mem_h memh) {
        ucp_request_param_t p{};
        p.op_attr_mask = UCP_OP_ATTR_FIELD_MEMH;
        p.memh = memh;
        return ucp_put_nbx(ep_, local, n, remote, rkey_, &p);
    }

    bool flush() {
        ucp_request_param_t p{};
        return wait(ucp_ep_flush_nbx(ep_, &p), "ucp_ep_flush_nbx");
    }

    void print_info() {
        ucp_ep_print_info(ep_, stdout); // the lanes: transport evidence (evidence.py)
        std::fflush(stdout);
    }

    bool wait(ucs_status_ptr_t request, const char* what) {
        if (request == nullptr) {
            return true;
        }
        if (UCS_PTR_IS_ERR(request)) {
            return ok(UCS_PTR_STATUS(request), what);
        }
        ucs_status_t s = UCS_INPROGRESS;
        while ((s = ucp_request_check_status(request)) == UCS_INPROGRESS) {
            progress();
        }
        ucp_request_free(request);
        return ok(s, what);
    }

  private:
    ucp_context_h ctx_ = nullptr;
    ucp_worker_h worker_ = nullptr;
    ucp_ep_h ep_ = nullptr;
    ucp_rkey_h rkey_ = nullptr;
    std::vector<ucp_mem_h> maps_;
    bool ready_ = false;
};

// Puts [begin, begin + bytes) of the local buffer to the same offsets of the target, in
// `chunk` sized puts with at most `inflight` outstanding.
class PutStream {
  public:
    PutStream(Session& s, const Memory& local, ucp_mem_h memh, std::uint64_t remote_base,
              std::uint64_t begin, std::uint64_t bytes, std::uint64_t chunk, int inflight)
        : s_(s), local_(local), memh_(memh), remote_(remote_base), next_(begin),
          end_(begin + bytes), chunk_(chunk == 0 ? bytes : chunk),
          inflight_(static_cast<std::size_t>(inflight)) {}

    // Issues puts while the window has room, then progresses once.
    bool step() {
        while (next_ < end_ && pending_.size() < inflight_) {
            const auto n = std::min(chunk_, end_ - next_);
            auto r = s_.put(local_.data() + next_, n, remote_ + next_, memh_);
            if (UCS_PTR_IS_ERR(r)) {
                return ok(UCS_PTR_STATUS(r), "ucp_put_nbx");
            }
            if (r != nullptr) {
                pending_.push_back(r);
            }
            next_ += n;
        }
        s_.progress();
        for (std::size_t i = 0; i < pending_.size();) {
            const auto st = ucp_request_check_status(pending_[i]);
            if (st == UCS_INPROGRESS) {
                ++i;
                continue;
            }
            ucp_request_free(pending_[i]);
            pending_.erase(pending_.begin() + static_cast<std::ptrdiff_t>(i));
            if (!ok(st, "ucp_put_nbx")) {
                return false;
            }
        }
        return true;
    }
    [[nodiscard]] bool done() const { return next_ >= end_ && pending_.empty(); }

  private:
    Session& s_;
    const Memory& local_;
    ucp_mem_h memh_;
    std::uint64_t remote_;
    std::uint64_t next_;
    std::uint64_t end_;
    std::uint64_t chunk_;
    std::size_t inflight_;
    std::vector<ucs_status_ptr_t> pending_;
};

constexpr std::uint32_t kSeed = 5;

struct Options {
    int rank = -1;
    bool smoke = false;
    bool cuda = false;
    std::uint64_t bytes = 0; // per path
    std::uint64_t chunk = 0;
    int inflight = 1;
    std::vector<std::string> devices; // one session per entry ("" = UCX's choice)
    std::string tls;
};

int parse_rank(const Args& a) {
    if (a.flag("listen")) {
        return 0;
    }
    if (a.flag("connect")) {
        return 1;
    }
    if (a.num("size", 2) != 2) {
        return -1;
    }
    return static_cast<int>(a.num("rank", -1));
}

#ifdef OSTIA_BENCH_CUDA
bool have_cuda_device() {
    int n = 0;
    return cudaGetDeviceCount(&n) == cudaSuccess && n > 0;
}
#endif

// Returns 0 to continue, or an exit code.
int check_options(const Options& o, const std::string& bench) {
    if (o.rank != 0 && o.rank != 1) {
        return fail(bench + " runs as two ranks",
                    "use fabric/tests/multiprocess/launcher.py --ranks 2, or --listen PORT on "
                    "the target and --connect HOST:PORT on the source");
    }
    if (o.cuda) {
#ifndef OSTIA_BENCH_CUDA
        return fail(bench + " --mem cuda needs a CUDA build",
                    "build with the cuda-12 or cuda-13 environment, or pass --mem host");
#else
        if (!have_cuda_device()) {
            std::printf("skipped: no CUDA device\n");
            return kSkip;
        }
#endif
    }
    return 0;
}

int target(Channel& ch, const Options& o, const Args& a, const std::string& bench) {
    std::vector<std::unique_ptr<Session>> sessions;
    for (const auto& d : o.devices) {
        sessions.push_back(std::make_unique<Session>(o.tls, d));
        if (!sessions.back()->ready()) {
            return 1;
        }
    }
    const std::uint64_t total = o.bytes * sessions.size();
    Memory region(total, o.cuda);
    if (region.data() == nullptr) {
        return fail("cannot allocate " + std::to_string(total) + " bytes", "lower --bytes");
    }
    region.zero();
    std::string info;
    put_u64(info, sessions.size());
    ucp_mem_h first = nullptr;
    for (auto& s : sessions) {
        auto* memh = s->map(region);
        if (memh == nullptr) {
            return 1;
        }
        first = first == nullptr ? memh : first;
        put_str(info, s->address());
        put_str(info, s->rkey(memh));
    }
    put_u64(info, reinterpret_cast<std::uint64_t>(region.data()));
    if (!ch.send(info)) {
        return 1;
    }
    evidence("target_memory_type=" + Session::memory_type(first));
    const auto progress = [&] {
        for (auto& s : sessions) {
            s->progress();
        }
    };
    int rc = 0;
    std::string cmd;
    while (ch.recv(cmd, progress)) {
        if (cmd.empty() || cmd[0] == 'Q') {
            return rc;
        }
        if (cmd[0] == 'V') { // verify [0, n)
            Reader r{cmd, 1};
            const auto n = r.u64();
            auto data = region.read();
            if (a.flag("corrupt")) {
                data[n / 2] ^= 0xffU; // self-check: the checksum must notice
            }
            const auto bad = count_mismatches(data.data(), n, kSeed);
            std::string reply;
            put_u64(reply, bad);
            if (!ch.send(reply)) {
                return 1;
            }
            if (bad != 0) {
                rc = checksum_failed(bench, bad, n);
            }
        }
    }
    return 1;
}

struct SourceRun {
    std::vector<std::unique_ptr<Session>> sessions;
    std::unique_ptr<Memory> local;
    std::vector<ucp_mem_h> memh;
    std::uint64_t remote = 0;
    std::uint64_t moved = 0;
};

// Connects to the target: returns false on failure.
bool connect_source(Channel& ch, const Options& o, SourceRun& run) {
    std::string info;
    if (!ch.recv(info, [] {})) {
        return false;
    }
    Reader r{info};
    const auto n = r.u64();
    if (n != o.devices.size()) {
        std::fprintf(stderr, "error: the target has %llu rails, the source %zu\n",
                     static_cast<unsigned long long>(n), o.devices.size());
        return false;
    }
    run.local = std::make_unique<Memory>(o.bytes * n, o.cuda);
    if (run.local->data() == nullptr) {
        std::fprintf(stderr, "error: cannot allocate the source buffer\n");
        return false;
    }
    run.local->fill(kSeed);
    for (const auto& d : o.devices) {
        auto s = std::make_unique<Session>(o.tls, d);
        const auto address = r.str();
        const auto rkey = r.str();
        if (!s->ready() || !s->connect(address, rkey)) {
            return false;
        }
        run.memh.push_back(s->map(*run.local));
        run.sessions.push_back(std::move(s));
    }
    run.remote = r.u64();
    return true;
}

// One timed transfer of every path in `paths` at once; returns GB/s, or a negative value.
double transfer(const Options& o, SourceRun& run, const std::vector<std::size_t>& paths) {
    std::vector<PutStream> streams;
    streams.reserve(paths.size());
    for (auto p : paths) {
        streams.emplace_back(*run.sessions[p], *run.local, run.memh[p], run.remote, p * o.bytes,
                             o.bytes, o.chunk, o.inflight);
    }
    const Timer t;
    bool all_done = false;
    while (!all_done) {
        all_done = true;
        for (auto& s : streams) {
            if (!s.done()) {
                if (!s.step()) {
                    return -1;
                }
                all_done = all_done && s.done();
            }
        }
    }
    for (auto p : paths) {
        if (!run.sessions[p]->flush()) {
            return -1;
        }
    }
    const double seconds = t.seconds();
    run.moved += o.bytes * paths.size();
    return gbps(o.bytes * paths.size(), seconds);
}

bool measure(const Options& o, const Args& a, SourceRun& run, const std::vector<std::size_t>& paths,
             std::vector<double>& rates) {
    for (int i = 0; i < 2 + samples(a); ++i) {
        const double r = transfer(o, run, paths);
        if (r < 0) {
            return false;
        }
        if (i >= 2) {
            rates.push_back(r);
        }
    }
    return true;
}

// Asks the target to checksum [0, n); true when every byte matches.
bool verified(Channel& ch, SourceRun& run, std::uint64_t n, const std::string& bench) {
    std::string cmd = "V";
    put_u64(cmd, n);
    std::string reply;
    if (!ch.send(cmd) || !ch.recv(reply, [&] {
            for (auto& s : run.sessions) {
                s->progress();
            }
        })) {
        return false;
    }
    Reader r{reply};
    const auto bad = r.u64();
    if (bad != 0) {
        checksum_failed(bench, bad, n);
        return false;
    }
    return true;
}

// Closes the endpoints, then lets the target quit.
void finish(Channel& ch, SourceRun& run) {
    for (auto& s : run.sessions) {
        s->close();
    }
    ch.send("Q");
}

std::vector<std::string> split(const std::string& s) {
    std::vector<std::string> out;
    std::size_t start = 0;
    while (start <= s.size()) {
        const auto comma = s.find(',', start);
        out.push_back(
            s.substr(start, comma == std::string::npos ? std::string::npos : comma - start));
        if (comma == std::string::npos) {
            break;
        }
        start = comma + 1;
    }
    return out;
}

} // namespace

int put_main(int argc, char** argv, const PutWorkload& w) {
    const Args a(argc, argv);
    Options o;
    o.rank = parse_rank(a);
    o.smoke = a.flag("smoke");
    o.cuda = a.str("mem", w.mem) == "cuda";
    o.bytes = a.size("bytes", o.smoke ? 16 * kMiB : kGiB);
    o.chunk = a.size("chunk", w.chunk);
    o.inflight = static_cast<int>(a.num("inflight", w.inflight));
    o.tls = w.tls;
    o.devices = {a.str("nic", "")};
    if (const int rc = check_options(o, w.bench); rc != 0) {
        return rc;
    }
    Channel ch;
    if (!ch.open(a, o.rank)) {
        return 1;
    }
    if (o.rank == 0) {
        return target(ch, o, a, w.bench);
    }
    SourceRun run;
    std::vector<double> rates;
    if (!connect_source(ch, o, run) || !measure(o, a, run, {0}, rates)) {
        return 1;
    }
    const bool good = verified(ch, run, o.bytes, w.bench);
    if (good) {
        run.sessions[0]->print_info();
    }
    finish(ch, run);
    if (!good) {
        return 1;
    }
    evidence("memory_type=" + Session::memory_type(run.memh[0]) +
             " bytes=" + std::to_string(run.moved));
    std::string params = "{\"bytes\": " + std::to_string(o.bytes);
    if (w.stream_params) {
        params += ", \"chunk\": " + std::to_string(o.chunk == 0 ? o.bytes : o.chunk) +
                  ", \"inflight\": " + std::to_string(o.inflight);
    }
    if (w.bench != "tcp_put") {
        params += std::string(", \"mem\": \"") + (o.cuda ? "cuda" : "host") + "\"";
    }
    emit(w.bench, params + "}", rates);
    return 0;
}

int rails_main(int argc, char** argv) {
    const Args a(argc, argv);
    Options o;
    o.rank = parse_rank(a);
    o.smoke = a.flag("smoke");
    o.cuda = a.str("mem", "cuda") == "cuda";
    o.bytes = a.size("bytes", o.smoke ? 16 * kMiB : kGiB);
    o.chunk = a.size("chunk", 4 * kMiB);
    o.inflight = static_cast<int>(a.num("inflight", 8));
    o.devices = split(a.str("nics", ""));
    if (o.devices.size() != 2) {
        return fail("dual_link --mode rails needs two NICs",
                    "pass --nics mlx5_0:1,mlx5_1:1 (see ibv_devinfo)");
    }
    if (const int rc = check_options(o, "dual_link"); rc != 0) {
        return rc;
    }
    Channel ch;
    if (!ch.open(a, o.rank)) {
        return 1;
    }
    if (o.rank == 0) {
        return target(ch, o, a, "dual_link");
    }
    SourceRun run;
    std::vector<double> path_a;
    std::vector<double> path_b;
    std::vector<double> both;
    if (!connect_source(ch, o, run) || !measure(o, a, run, {0}, path_a) ||
        !measure(o, a, run, {1}, path_b) || !measure(o, a, run, {0, 1}, both)) {
        return 1;
    }
    const bool good = verified(ch, run, 2 * o.bytes, "dual_link");
    if (good) {
        for (auto& s : run.sessions) {
            s->print_info();
        }
    }
    finish(ch, run);
    if (!good) {
        return 1;
    }
    evidence("mode=rails memory_type=" + Session::memory_type(run.memh[0]) +
             " nics=" + a.str("nics", "") + " bytes=" + std::to_string(run.moved));
    const std::string tail = ", \"bytes\": " + std::to_string(o.bytes) + ", \"mode\": \"rails\"}";
    emit("dual_link", "{\"path\": \"a\"" + tail, path_a);
    emit("dual_link", "{\"path\": \"b\"" + tail, path_b);
    emit("dual_link", "{\"path\": \"both\"" + tail, both);
    return 0;
}

} // namespace ostia::bench::ucx
