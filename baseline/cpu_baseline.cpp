// cpu_baseline -- the mdpipe pipeline written as plain, fast C++ for a CPU core.
//
// Purpose: an honest software comparison for the FPGA design.  It implements the
// SAME rules as model/mdp_model.py (header screening, ITCH-lite decode, drop
// reasons and their precedence, order table, price-level book, edge-armed
// trigger, the exact 60-byte order frame) and must reproduce the golden model's
// output bit-for-bit (--check).  Only then is its speed worth quoting.
//
//   cpu_baseline --check  vectors/        compare against expected.txt
//   cpu_baseline --bench  vectors/ [--pin N] [--passes N]
//
// What the benchmark measures: processing time for one frame that is ALREADY IN
// MEMORY (as if a NIC had DMA'd it) until the order frame has been built in
// memory.  No NIC, no kernel, no network stack, no PCIe.  The FPGA's 5-cycle
// figure likewise excludes MAC/PHY.  Both numbers are "core compute only".
// Anything a real CPU deployment adds on top (NIC DMA, PCIe, interrupts or
// polling, TX path) is NOT in here.
//
// Design choices that make this a strong baseline rather than a strawman:
// no allocation or I/O in the hot path, flat arrays, one 64-bit occupancy mask
// per {symbol, side} with ctz/clz for best price (the same structure as the
// RTL), branch-light parsing, a pre-built order-frame template.

#include <algorithm>
#include <chrono>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <fstream>
#include <sstream>
#include <string>
#include <vector>

#if defined(__x86_64__) || defined(_M_X64)
#include <x86intrin.h>
#define HAVE_TSC 1
#endif
#if defined(__linux__)
#include <sched.h>
#endif

namespace {

constexpr int SYM_W = 2, N_SYM = 1 << SYM_W;
constexpr int LVL_W = 6, N_LVL = 1 << LVL_W;
constexpr int ORD_AW = 10, N_ORD = 1 << ORD_AW;
constexpr uint8_t T_ADD = 0x41, T_DEL = 0x44, T_EXE = 0x45;
constexpr uint8_t SIDE_B = 0x42, SIDE_S = 0x53;
enum : uint8_t { R_BAD_SYM = 1, R_BAD_PRICE, R_DUP_REF, R_UNK_REF, R_BAD_TYPE, R_BAD_REF, R_BAD_QTY };
constexpr size_t PAYLOAD_OFF = 44, MSG_LEN = 16, ORDER_FRAME_LEN = 60;

struct Cfg {
    uint32_t base_px = 1000, buy_px = 1025, qty = 100;
    bool en = true;
    uint16_t md_dport = 26400, sport = 40000, o_dport = 9000;
    uint64_t dst_mac = 0x020000000002ULL, src_mac = 0x020000000001ULL;
    uint32_t src_ip = 0x0A000001, dst_ip = 0x0A000002;

    uint16_t ip_csum() const {
        uint32_t w[] = {0x4500, 0x002C, 0x0000, 0x4000, 0x4011, 0x0000,
                        src_ip >> 16, src_ip & 0xFFFF, dst_ip >> 16, dst_ip & 0xFFFF};
        uint32_t s = 0;
        for (uint32_t x : w) s += x;
        while (s >> 16) s = (s & 0xFFFF) + (s >> 16);
        return static_cast<uint16_t>(~s & 0xFFFF);
    }
};

inline uint16_t be16(const uint8_t* p) { return static_cast<uint16_t>((p[0] << 8) | p[1]); }
inline uint32_t be32(const uint8_t* p) {
    return (uint32_t(p[0]) << 24) | (uint32_t(p[1]) << 16) | (uint32_t(p[2]) << 8) | p[3];
}
inline void put16(uint8_t* p, uint16_t v) { p[0] = v >> 8; p[1] = v & 0xFF; }
inline void put32(uint8_t* p, uint32_t v) { put16(p, v >> 16); put16(p + 2, v & 0xFFFF); }

struct Order {
    uint32_t rem;
    uint8_t valid, sym, side, lvl;
};

template <class Sink>
class Pipeline {
public:
    explicit Pipeline(const Cfg& c) : c_(c) {
        std::memset(ord_, 0, sizeof ord_);
        std::memset(lvl_, 0, sizeof lvl_);
        std::memset(mask_, 0, sizeof mask_);
        std::memset(fired_, 0, sizeof fired_);
        // Order-frame template: everything constant is filled once.
        uint8_t* f = tmpl_;
        std::memset(f, 0, ORDER_FRAME_LEN);
        for (int i = 0; i < 6; i++) f[i] = (c_.dst_mac >> (8 * (5 - i))) & 0xFF;
        for (int i = 0; i < 6; i++) f[6 + i] = (c_.src_mac >> (8 * (5 - i))) & 0xFF;
        put16(f + 12, 0x0800);
        f[14] = 0x45; f[15] = 0; put16(f + 16, 44); put16(f + 18, 0); put16(f + 20, 0x4000);
        f[22] = 64; f[23] = 17; put16(f + 24, c_.ip_csum());
        put32(f + 26, c_.src_ip); put32(f + 30, c_.dst_ip);
        put16(f + 34, c_.sport); put16(f + 36, c_.o_dport); put16(f + 38, 24); put16(f + 40, 0);
        f[42] = 'O'; f[43] = 'B';
    }

    // `len` must be a multiple of 8 (frames are padded like the AXI-Stream beats).
    void frame(const uint8_t* f, size_t len, Sink& s) {
        const size_t nbeats = len / 8;
        if (nbeats < 7) { frm_bad++; return; }
        const bool ok = f[12] == 0x08 && f[13] == 0x00 && f[14] == 0x45 && f[23] == 17 &&
                        be16(f + 36) == c_.md_dport;
        if (!ok) { frm_bad++; return; }
        frm_good++;
        const size_t count = be16(f + 42);
        const size_t n = std::min(count, (nbeats - 6) / 2);
        for (size_t k = 0; k < n; k++) {
            const uint8_t* m = f + PAYLOAD_OFF + MSG_LEN * k;
            msgs++;
            apply(m[0], m[1], be16(m + 2), be32(m + 4), be32(m + 8), be32(m + 12), s);
        }
    }

    uint32_t frm_good = 0, frm_bad = 0, msgs = 0, drops = 0, orders = 0;

private:
    void drop(uint8_t r, Sink& s) { drops++; s.on_drop(r); }

    void apply(uint8_t t, uint8_t side, uint16_t sym, uint32_t ref, uint32_t px, uint32_t shs, Sink& s) {
        const bool add = t == T_ADD, del = t == T_DEL, exe = t == T_EXE;
        const bool type_ok = add ? (side == SIDE_B || side == SIDE_S) : (del || exe);
        if (!type_ok) return drop(R_BAD_TYPE, s);
        if (ref >> ORD_AW) return drop(R_BAD_REF, s);
        if (add) {
            if (sym >> SYM_W) return drop(R_BAD_SYM, s);
            if (px < c_.base_px || (px - c_.base_px) >= uint32_t(N_LVL)) return drop(R_BAD_PRICE, s);
        }
        if ((add || exe) && shs == 0) return drop(R_BAD_QTY, s);

        Order& o = ord_[ref];
        uint8_t usym;
        if (add) {
            if (o.valid) return drop(R_DUP_REF, s);
            const uint8_t sd = side == SIDE_S ? 1 : 0;
            const uint8_t lv = static_cast<uint8_t>(px - c_.base_px);
            o = Order{shs, 1, static_cast<uint8_t>(sym), sd, lv};
            uint32_t& q = lvl_[sym][sd][lv];
            q += shs;
            set_mask(sym, sd, lv, q);
            usym = static_cast<uint8_t>(sym);
        } else {
            if (!o.valid) return drop(R_UNK_REF, s);
            const uint32_t delta = (del || shs >= o.rem) ? o.rem : shs;
            uint32_t& q = lvl_[o.sym][o.side][o.lvl];
            q -= delta;
            set_mask(o.sym, o.side, o.lvl, q);
            o.rem -= delta;
            if (o.rem == 0) o.valid = 0;
            usym = o.sym;
        }
        post_update(usym, s);
    }

    void set_mask(unsigned sym, unsigned sd, unsigned lv, uint32_t q) {
        const uint64_t bit = 1ULL << lv;
        mask_[sym][sd] = q ? (mask_[sym][sd] | bit) : (mask_[sym][sd] & ~bit);
    }

    void post_update(uint8_t sym, Sink& s) {
        const uint64_t bm = mask_[sym][0], am = mask_[sym][1];
        const bool bv = bm != 0, av = am != 0;
        const uint32_t bp = bv ? c_.base_px + (63 - __builtin_clzll(bm)) : 0;
        const uint32_t ap = av ? c_.base_px + __builtin_ctzll(am) : 0;
        s.on_tob(sym, bv, bp, av, ap);
        const bool cond = c_.en && av && ap <= c_.buy_px;
        if (cond && !fired_[sym]) {
            uint8_t out[ORDER_FRAME_LEN];
            std::memcpy(out, tmpl_, ORDER_FRAME_LEN);
            put16(out + 44, sym);
            put32(out + 46, cid_++);
            put32(out + 50, ap);
            put32(out + 54, c_.qty);
            fired_[sym] = true;
            orders++;
            s.on_order(out);
        } else if (!cond) {
            fired_[sym] = false;
        }
    }

    Cfg c_;
    Order ord_[N_ORD];
    uint32_t lvl_[N_SYM][2][N_LVL];
    uint64_t mask_[N_SYM][2];
    bool fired_[N_SYM];
    uint32_t cid_ = 1;
    uint8_t tmpl_[ORDER_FRAME_LEN];
};

// ---------------------------------------------------------------- sinks
struct CheckSink {  // formats events exactly like expected.txt
    std::vector<std::string> tob, drop, ord;
    void on_tob(unsigned sym, bool bv, uint32_t bp, bool av, uint32_t ap) {
        char b[96];
        std::snprintf(b, sizeof b, "TOB %u %d %u %d %u", sym, bv, bp, av, ap);
        tob.emplace_back(b);
    }
    void on_drop(unsigned r) { drop.emplace_back("DROP " + std::to_string(r)); }
    void on_order(const uint8_t* f) {
        static const char* hx = "0123456789abcdef";
        std::string s = "ORD ";
        for (size_t i = 0; i < ORDER_FRAME_LEN; i++) { s += hx[f[i] >> 4]; s += hx[f[i] & 15]; }
        ord.push_back(std::move(s));
    }
};

struct BenchSink {  // does the minimum that cannot be optimised away
    uint64_t sum = 0;
    inline void on_tob(unsigned sym, bool bv, uint32_t bp, bool av, uint32_t ap) {
        sum += sym + bv + bp + av + ap;
    }
    inline void on_drop(unsigned r) { sum += r; }
    inline void on_order(const uint8_t* f) {
        uint64_t x; std::memcpy(&x, f + 44, 8); sum ^= x;
        std::memcpy(&x, f + 52, 8); sum ^= x;
    }
};

// ---------------------------------------------------------------- vectors
struct Vectors {
    Cfg cfg;
    std::vector<uint8_t> arena;                 // all frames, each padded to 8 bytes
    std::vector<std::pair<size_t, size_t>> fr;  // (offset, padded length)
    std::vector<std::string> tob, drop, ord;
    uint32_t c_good = 0, c_bad = 0, c_msgs = 0, c_drops = 0, c_orders = 0;
};

bool load(const std::string& dir, Vectors& v) {
    std::ifstream fb(dir + "/frames.bin", std::ios::binary);
    if (!fb) { std::fprintf(stderr, "cannot open %s/frames.bin\n", dir.c_str()); return false; }
    uint32_t len;
    std::vector<uint8_t> tmp;
    while (fb.read(reinterpret_cast<char*>(&len), 4)) {
        tmp.resize(len);
        fb.read(reinterpret_cast<char*>(tmp.data()), len);
        const size_t padded = (len + 7) & ~size_t(7);
        tmp.resize(padded, 0);
        v.fr.emplace_back(v.arena.size(), padded);
        v.arena.insert(v.arena.end(), tmp.begin(), tmp.end());
    }
    std::ifstream fe(dir + "/expected.txt");
    if (!fe) { std::fprintf(stderr, "cannot open %s/expected.txt\n", dir.c_str()); return false; }
    std::string line;
    while (std::getline(fe, line)) {
        std::istringstream is(line);
        std::string tag;
        is >> tag;
        if (tag == "CFG") {
            std::string k; unsigned long long x;
            is >> k >> x;
            if (k == "base_px") v.cfg.base_px = x;
            else if (k == "buy_px") v.cfg.buy_px = x;
            else if (k == "qty") v.cfg.qty = x;
            else if (k == "en") v.cfg.en = x;
            else if (k == "md_dport") v.cfg.md_dport = x;
            else if (k == "dst_mac") v.cfg.dst_mac = x;
            else if (k == "src_mac") v.cfg.src_mac = x;
            else if (k == "src_ip") v.cfg.src_ip = x;
            else if (k == "dst_ip") v.cfg.dst_ip = x;
            else if (k == "sport") v.cfg.sport = x;
            else if (k == "o_dport") v.cfg.o_dport = x;
        } else if (tag == "TOB") v.tob.push_back(line);
        else if (tag == "DROP") v.drop.push_back(line);
        else if (tag == "ORD") v.ord.push_back(line);
        else if (tag == "COUNTS") is >> v.c_good >> v.c_bad >> v.c_msgs >> v.c_drops >> v.c_orders;
    }
    return true;
}

int check(const Vectors& v) {
    Pipeline<CheckSink> p(v.cfg);
    CheckSink s;
    for (auto [off, len] : v.fr) p.frame(v.arena.data() + off, len, s);
    int bad = 0;
    auto cmp = [&](const char* name, const std::vector<std::string>& got, const std::vector<std::string>& exp) {
        if (got.size() != exp.size()) {
            std::printf("FAIL %s: count cpu=%zu model=%zu\n", name, got.size(), exp.size());
            bad++;
            return;
        }
        for (size_t i = 0; i < got.size(); i++)
            if (got[i] != exp[i]) {
                std::printf("FAIL %s #%zu:\n  cpu   =%s\n  model =%s\n", name, i, got[i].c_str(), exp[i].c_str());
                bad++;
                return;
            }
        std::printf("ok   %-5s %zu events identical\n", name, got.size());
    };
    cmp("TOB", s.tob, v.tob);
    cmp("DROP", s.drop, v.drop);
    cmp("ORD", s.ord, v.ord);
    if (p.frm_good != v.c_good || p.frm_bad != v.c_bad || p.msgs != v.c_msgs ||
        p.drops != v.c_drops || p.orders != v.c_orders) {
        std::printf("FAIL counts cpu=%u/%u/%u/%u/%u model=%u/%u/%u/%u/%u\n", p.frm_good, p.frm_bad, p.msgs,
                    p.drops, p.orders, v.c_good, v.c_bad, v.c_msgs, v.c_drops, v.c_orders);
        bad++;
    } else {
        std::printf("ok   counts identical\n");
    }
    std::printf(bad ? "CHECK FAILED\n" : "CHECK PASSED: C++ output is bit-exact with the golden model\n");
    return bad ? 1 : 0;
}

// ---------------------------------------------------------------- benchmark
inline uint64_t now_ticks() {
#ifdef HAVE_TSC
    unsigned aux;
    return __rdtscp(&aux);
#else
    return std::chrono::steady_clock::now().time_since_epoch().count();
#endif
}

double ticks_per_ns() {
#ifdef HAVE_TSC
    using clk = std::chrono::steady_clock;
    auto t0 = clk::now(); uint64_t c0 = now_ticks();
    while (clk::now() - t0 < std::chrono::milliseconds(300)) {}
    auto t1 = clk::now(); uint64_t c1 = now_ticks();
    return double(c1 - c0) / std::chrono::duration<double, std::nano>(t1 - t0).count();
#else
    return 1.0;  // steady_clock ticks are ns on common platforms
#endif
}

double pct(std::vector<double>& v, double p) {
    if (v.empty()) return 0;
    size_t i = std::min(v.size() - 1, size_t(p / 100.0 * v.size()));
    return v[i];
}

int bench(const Vectors& v, int pin, int passes) {
#if defined(__linux__)
    if (pin >= 0) {
        cpu_set_t set; CPU_ZERO(&set); CPU_SET(pin, &set);
        if (sched_setaffinity(0, sizeof set, &set) != 0) std::perror("sched_setaffinity");
    }
#endif
    const double tpn = ticks_per_ns();
    // timer overhead: back-to-back reads
    uint64_t ovh = ~0ULL;
    for (int i = 0; i < 100000; i++) { uint64_t a = now_ticks(), b = now_ticks(); ovh = std::min(ovh, b - a); }

    BenchSink sink;
    auto run_pass = [&] {
        Pipeline<BenchSink> p(v.cfg);
        for (auto [off, len] : v.fr) p.frame(v.arena.data() + off, len, sink);
        return p.msgs;
    };
    uint32_t msgs_per_pass = 0;
    for (int i = 0; i < 3; i++) msgs_per_pass = run_pass();  // warm caches / branch predictors

    // throughput: no per-frame timing inside the loop
    auto t0 = std::chrono::steady_clock::now();
    for (int i = 0; i < passes; i++) run_pass();
    auto t1 = std::chrono::steady_clock::now();
    double secs = std::chrono::duration<double>(t1 - t0).count();

    // latency: per-frame timing, order-producing frames reported separately
    std::vector<double> lat_all, lat_ord;
    for (int i = 0; i < passes; i++) {
        Pipeline<BenchSink> p(v.cfg);
        for (auto [off, len] : v.fr) {
            const uint32_t before = p.orders;
            uint64_t a = now_ticks();
            p.frame(v.arena.data() + off, len, sink);
            uint64_t b = now_ticks();
            double ns = double(b - a - std::min(b - a, ovh)) / tpn;
            lat_all.push_back(ns);
            if (p.orders != before) lat_ord.push_back(ns);
        }
    }
    std::sort(lat_all.begin(), lat_all.end());
    std::sort(lat_ord.begin(), lat_ord.end());

    std::printf("frames/pass=%zu  messages/pass=%u  passes=%d  (sink=%llu)\n", v.fr.size(), msgs_per_pass,
                passes, (unsigned long long)sink.sum);
    std::printf("timer: %s, %.3f ticks/ns, overhead %.1f ns (subtracted)\n",
#ifdef HAVE_TSC
                "rdtscp",
#else
                "steady_clock",
#endif
                tpn, ovh / tpn);
    std::printf("throughput: %.2f M msgs/s, %.2f M frames/s  (%.1f ns/msg amortised)\n",
                double(msgs_per_pass) * passes / secs / 1e6, double(v.fr.size()) * passes / secs / 1e6,
                secs * 1e9 / (double(msgs_per_pass) * passes));
    auto row = [&](const char* name, std::vector<double>& x) {
        std::printf("%-26s n=%-8zu p50=%7.1f  p90=%7.1f  p99=%7.1f  p99.9=%7.1f  max=%8.1f ns\n", name, x.size(),
                    pct(x, 50), pct(x, 90), pct(x, 99), pct(x, 99.9), x.empty() ? 0.0 : x.back());
    };
    row("frame -> done (all)", lat_all);
    row("frame -> order built", lat_ord);
    std::printf("\nScope: frame already in cache-warm memory; order built in memory. No NIC, kernel, PCIe or\n"
                "TX path. Multi-message frames: the last column includes every message ahead of the trigger.\n");
    return 0;
}

}  // namespace

int main(int argc, char** argv) {
    std::string mode, dir;
    int pin = -1, passes = 50;
    for (int i = 1; i < argc; i++) {
        std::string a = argv[i];
        if (a == "--check" || a == "--bench") mode = a;
        else if (a == "--pin" && i + 1 < argc) pin = std::atoi(argv[++i]);
        else if (a == "--passes" && i + 1 < argc) passes = std::atoi(argv[++i]);
        else dir = a;
    }
    if (mode.empty() || dir.empty()) {
        std::fprintf(stderr, "usage: %s --check|--bench <vectors-dir> [--pin CPU] [--passes N]\n", argv[0]);
        return 2;
    }
    Vectors v;
    if (!load(dir, v)) return 2;
    return mode == "--check" ? check(v) : bench(v, pin, passes);
}
